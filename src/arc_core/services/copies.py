from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select

from arc_core.catalog_models import (
    CollectionFileRecord,
    FileCopyRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
)
from arc_core.domain.errors import Conflict, NotFound, NotYetImplemented
from arc_core.domain.models import CopySummary
from arc_core.domain.types import CopyId
from arc_core.planner.manifest import MANIFEST_FILENAME
from arc_core.ports.hot_store import HotStore
from arc_core.recovery_payloads import decrypt_recovery_payload
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope

_ENC_JSON = json.dumps({"alg": "fixture-age-plugin-batchpass/v1"}, sort_keys=True)


class SqlAlchemyCopyService:
    def __init__(self, config: RuntimeConfig, hot_store: HotStore) -> None:
        self._config = config
        self._hot_store = hot_store
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        with session_scope(self._session_factory) as session:
            image = session.get(FinalizedImageRecord, image_id)
            if image is None:
                raise NotFound(f"image not found: {image_id}")
            existing = session.get(ImageCopyRecord, {"image_id": image_id, "copy_id": copy_id})
            if existing is not None:
                raise Conflict(f"copy already exists for volume: {copy_id}")
            created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            session.add(
                ImageCopyRecord(
                    image_id=image_id,
                    copy_id=copy_id,
                    location=location,
                    created_at=created_at,
                )
            )
            covered = session.scalars(
                select(FinalizedImageCoveredPathRecord).where(
                    FinalizedImageCoveredPathRecord.image_id == image_id
                )
            ).all()
            disc_entries = _read_disc_manifest_entries(image.image_root)
            for cp in covered:
                file_record = session.get(
                    CollectionFileRecord,
                    {"collection_id": cp.collection_id, "path": cp.path},
                )
                if file_record is not None:
                    file_record.archived = True
                for disc_path, part_index, part_count in disc_entries.get(
                    (cp.collection_id, cp.path), []
                ):
                    part_bytes_val = None
                    part_sha256_val = None
                    if part_count > 1:
                        assert part_index is not None
                        content = self._hot_store.get_collection_file(
                            cp.collection_id, cp.path
                        )
                        parts = _split_plaintext(content, part_count)
                        part_bytes_val = len(parts[part_index])
                        part_sha256_val = hashlib.sha256(parts[part_index]).hexdigest()
                    session.add(
                        FileCopyRecord(
                            collection_id=cp.collection_id,
                            path=cp.path,
                            copy_id=copy_id,
                            volume_id=image_id,
                            location=location,
                            disc_path=disc_path,
                            enc_json=_ENC_JSON,
                            part_index=part_index,
                            part_count=part_count if part_count > 1 else None,
                            part_bytes=part_bytes_val,
                            part_sha256=part_sha256_val,
                        )
                    )
            return CopySummary(
                id=CopyId(copy_id),
                volume_id=image_id,
                location=location,
                created_at=created_at,
            )


class StubCopyService:
    def register(self, image_id: str, copy_id: str, location: str) -> object:
        raise NotYetImplemented("StubCopyService is not implemented yet")


def _read_disc_manifest_entries(
    image_root: str,
) -> dict[tuple[str, str], list[tuple[str, int | None, int]]]:
    manifest_path = Path(image_root) / MANIFEST_FILENAME
    manifest = yaml.safe_load(decrypt_recovery_payload(manifest_path.read_bytes()))
    result: dict[tuple[str, str], list[tuple[str, int | None, int]]] = {}
    for collection in manifest.get("collections", []):
        collection_id = str(collection["id"])
        for file_entry in collection.get("files", []):
            path = str(file_entry["path"]).lstrip("/")
            parts_block = file_entry.get("parts")
            if parts_block is None:
                items: list[tuple[str, int | None, int]] = [
                    (str(file_entry["object"]), None, 1)
                ]
            else:
                part_count = int(parts_block["count"])
                items = [
                    (str(p["object"]), int(p["index"]) - 1, part_count)
                    for p in parts_block.get("present", [])
                ]
            result[(collection_id, path)] = items
    return result


def _split_plaintext(content: bytes, part_count: int) -> tuple[bytes, ...]:
    base, remainder = divmod(len(content), part_count)
    offset = 0
    parts: list[bytes] = []
    for i in range(part_count):
        size = base + int(i < remainder)
        parts.append(content[offset : offset + size])
        offset += size
    return tuple(parts)
