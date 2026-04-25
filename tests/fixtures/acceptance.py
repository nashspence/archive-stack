from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn

from arc_api.app import create_app
from arc_api.deps import ServiceContainer, get_container
from arc_core.domain.enums import FetchState
from arc_core.domain.errors import Conflict, HashMismatch, InvalidState, NotFound
from arc_core.domain.models import (
    CollectionSummary,
    CopySummary,
    FetchCopyHint,
    FetchSummary,
    PinSummary,
)
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import (
    CollectionId,
    CopyId,
    EntryId,
    FetchId,
    ImageId,
    Sha256Hex,
    TargetStr,
)
from arc_core.fs_paths import (
    derive_collection_id_from_staging_path,
    find_collection_id_conflict,
    normalize_collection_id,
)
from arc_core.services.planning import ImageRootPlanningService, ImageRootRecord
from tests.fixtures.data import (
    DEFAULT_COPY_CREATED_AT,
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    MIN_FILL_BYTES,
    PHOTOS_2024_FILES,
    PHOTOS_COLLECTION_ID,
    PHOTOS_NESTED_COLLECTION_ID,
    PHOTOS_PARENT_COLLECTION_ID,
    SPLIT_COPY_ONE_ID,
    SPLIT_COPY_ONE_LOCATION,
    SPLIT_COPY_TWO_ID,
    SPLIT_COPY_TWO_LOCATION,
    SPLIT_FILE_PARTS,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    TARGET_BYTES,
    build_file_copy,
    fixture_decrypt_bytes,
    fixture_encrypt_bytes,
    split_fixture_plaintext,
    staging_path_for_collection,
    write_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
FIXTURE_UPLOAD_EXPIRES_AT = "2099-12-31T23:59:59Z"
FIXTURE_UPLOAD_URL_BASE = "https://uploads.fixture.invalid"


@dataclass(frozen=True, slots=True)
class FileCopy:
    id: CopyId
    volume_id: str
    location: str
    disc_path: str
    enc: dict[str, object]
    part_index: int | None = None
    part_count: int | None = None
    part_bytes: int | None = None
    part_sha256: str | None = None

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, volume_id=self.volume_id, location=self.location)


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool
    archived: bool
    copies: list[FileCopy] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        return cast(Sha256Hex, hashlib.sha256(self.content).hexdigest())

    @property
    def projected_target(self) -> str:
        return f"{self.collection_id}/{self.path}"


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_id: ImageId
    finalized_id: str
    filename: str
    image_root: Path
    bytes: int
    iso_ready: bool
    covered_paths: tuple[tuple[CollectionId, str], ...]

    @property
    def files(self) -> int:
        return len(self.covered_paths)

    @property
    def collections(self) -> list[str]:
        return sorted({str(collection_id) for collection_id, _ in self.covered_paths})

    @property
    def projected_paths(self) -> list[str]:
        return sorted(f"{collection_id}/{path}" for collection_id, path in self.covered_paths)

    @property
    def fill(self) -> float:
        return self.bytes / TARGET_BYTES

    @property
    def finalized_at(self) -> str:
        return (
            f"{self.finalized_id[0:4]}-{self.finalized_id[4:6]}-{self.finalized_id[6:8]}"
            f"T{self.finalized_id[9:11]}:{self.finalized_id[11:13]}:{self.finalized_id[13:15]}Z"
        )

    def plan_payload(self) -> dict[str, object]:
        return {
            "candidate_id": str(self.candidate_id),
            "bytes": self.bytes,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": self.iso_ready,
        }

    def finalized_image_payload(self, *, copy_count: int = 0) -> dict[str, object]:
        return {
            "id": self.finalized_id,
            "filename": self.filename,
            "finalized_at": self.finalized_at,
            "bytes": self.bytes,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": True,
            "copy_count": copy_count,
        }

    def image_root_record(self) -> ImageRootRecord:
        return ImageRootRecord(
            image_id=self.finalized_id,
            volume_id=self.finalized_id,
            filename=self.filename,
            image_root=self.image_root,
        )


@dataclass(slots=True)
class FetchEntryRecord:
    id: EntryId
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
    content: bytes
    copies: list[FileCopy]
    uploaded_bytes: int = 0
    uploaded_content: bytes | None = None
    upload_expires_at: str | None = None
    upload_url: str | None = None


@dataclass(slots=True)
class FetchRecord:
    summary: FetchSummary
    entries: dict[EntryId, FetchEntryRecord]


@dataclass(slots=True)
class AcceptanceState:
    staged_directories: dict[str, Path] = field(default_factory=dict)
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    candidates_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    finalized_images_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    copy_summaries: dict[tuple[str, CopyId], CopySummary] = field(default_factory=dict)
    exact_pins: set[TargetStr] = field(default_factory=set)
    fetches: dict[FetchId, FetchRecord] = field(default_factory=dict)
    next_fetch_number: int = 0

    def register_staged_directory(self, staging_path: str, root: Path) -> None:
        self.staged_directories[staging_path] = root

    def seed_collection(
        self,
        collection_id: str,
        files: Mapping[str, bytes],
        *,
        hot_paths: set[str],
        archived_paths: set[str],
        copies_by_path: Mapping[str, list[dict[str, object]]] | None = None,
    ) -> None:
        copies_by_path = copies_by_path or {}
        normalized_collection_id = normalize_collection_id(collection_id)
        conflict = find_collection_id_conflict(
            (str(current) for current in self.files_by_collection), normalized_collection_id
        )
        if (
            CollectionId(normalized_collection_id) not in self.files_by_collection
            and conflict is not None
        ):
            raise Conflict(f"collection id conflicts with existing collection: {conflict}")
        collection_key = CollectionId(normalized_collection_id)
        records: dict[str, StoredFile] = {}
        for relative_path, content in sorted(files.items()):
            normalized = relative_path.lstrip("/")
            records[normalized] = StoredFile(
                collection_id=collection_key,
                path=normalized,
                content=content,
                hot=normalized in hot_paths,
                archived=normalized in archived_paths,
                copies=[self._copy_from_dict(item) for item in copies_by_path.get(normalized, [])],
            )
        self.files_by_collection[collection_key] = records

    def seed_image(self, image: CandidateRecord) -> None:
        self.candidates_by_id[image.candidate_id] = image

    def collection_files(self, collection_id: str | CollectionId) -> list[StoredFile]:
        collection_key = CollectionId(str(collection_id))
        records = self.files_by_collection.get(collection_key)
        if records is None:
            raise NotFound(f"collection not found: {collection_key}")
        return list(records.values())

    def file_content(self, collection_id: str | CollectionId, path: str) -> bytes:
        collection_key = CollectionId(str(collection_id))
        records = self.files_by_collection.get(collection_key)
        if records is None:
            raise NotFound(f"collection not found: {collection_key}")
        record = records.get(path)
        if record is None:
            raise NotFound(f"file not found in {collection_key}: {path}")
        return record.content

    def collection_summary(self, collection_id: str | CollectionId) -> CollectionSummary:
        records = self.collection_files(collection_id)
        return CollectionSummary(
            id=CollectionId(str(collection_id)),
            files=len(records),
            bytes=sum(record.bytes for record in records),
            hot_bytes=sum(record.bytes for record in records if record.hot),
            archived_bytes=sum(record.bytes for record in records if record.archived),
        )

    def selected_files(self, raw_target: str, *, missing_ok: bool = False) -> list[StoredFile]:
        target = parse_target(raw_target)
        selected = [
            record
            for collection_files in self.files_by_collection.values()
            for record in collection_files.values()
            if (
                record.projected_target.startswith(target.canonical)
                if target.is_dir
                else record.projected_target == target.canonical
            )
        ]
        if not selected and not missing_ok:
            raise NotFound(f"target not found: {raw_target}")
        return selected

    def selected_bytes(self, raw_target: str) -> int:
        return sum(record.bytes for record in self.selected_files(raw_target))

    def is_hot(self, raw_target: str) -> bool:
        selected = self.selected_files(raw_target, missing_ok=True)
        return bool(selected) and all(record.hot for record in selected)

    def reconcile_hot_from_pins(self) -> None:
        selected_paths: set[tuple[CollectionId, str]] = set()
        for raw_target in self.exact_pins:
            for record in self.selected_files(str(raw_target), missing_ok=True):
                selected_paths.add((record.collection_id, record.path))
        for collection_files in self.files_by_collection.values():
            for record in collection_files.values():
                record.hot = (record.collection_id, record.path) in selected_paths

    def reserve_fetch_id(self, fetch_id: str) -> None:
        if fetch_id.startswith("fx-"):
            suffix = fetch_id.removeprefix("fx-")
            if suffix.isdigit():
                self.next_fetch_number = max(self.next_fetch_number, int(suffix))

    @staticmethod
    def _copy_from_dict(item: dict[str, object]) -> FileCopy:
        return FileCopy(
            id=CopyId(str(item["id"])),
            volume_id=str(item["volume_id"]),
            location=str(item["location"]),
            disc_path=str(item["disc_path"]),
            enc=cast(dict[str, object], item["enc"]),
            part_index=cast(int | None, item.get("part_index")),
            part_count=cast(int | None, item.get("part_count")),
            part_bytes=cast(int | None, item.get("part_bytes")),
            part_sha256=cast(str | None, item.get("part_sha256")),
        )


class AcceptanceCollectionService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def close(self, staging_path: str) -> CollectionSummary:
        root = self.state.staged_directories.get(staging_path)
        if root is None:
            raise NotFound(f"staged directory not found: {staging_path}")
        collection_id = derive_collection_id_from_staging_path(staging_path)
        if CollectionId(collection_id) in self.state.files_by_collection:
            raise Conflict(f"collection already exists: {collection_id}")
        conflict = find_collection_id_conflict(
            (str(current) for current in self.state.files_by_collection), collection_id
        )
        if conflict is not None:
            raise Conflict(f"collection id conflicts with existing collection: {conflict}")
        files = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
        hot_paths = set(files)
        self.state.seed_collection(
            collection_id,
            files,
            hot_paths=hot_paths,
            archived_paths=set(),
        )
        return self.state.collection_summary(collection_id)

    def get(self, collection_id: str) -> CollectionSummary:
        return self.state.collection_summary(collection_id)


class AcceptanceSearchService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def search(self, query: str, limit: int) -> list[dict[str, object]]:
        needle = query.casefold()
        results: list[dict[str, object]] = []

        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            if needle in collection_name.casefold():
                summary = self.state.collection_summary(collection_id)
                results.append(
                    {
                        "kind": "collection",
                        "target": f"{collection_name}/",
                        "collection": collection_name,
                        "files": summary.files,
                        "bytes": summary.bytes,
                        "hot_bytes": summary.hot_bytes,
                        "archived_bytes": summary.archived_bytes,
                        "pending_bytes": summary.pending_bytes,
                    }
                )

        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            for record in sorted(
                self.state.collection_files(collection_id), key=lambda item: item.path
            ):
                full_path = record.projected_target
                if needle not in full_path.casefold():
                    continue
                results.append(
                    {
                        "kind": "file",
                        "target": record.projected_target,
                        "collection": collection_name,
                        "path": f"/{record.path}",
                        "bytes": record.bytes,
                        "hot": record.hot,
                        "copies": [
                            {
                                "id": str(copy.id),
                                "volume_id": copy.volume_id,
                                "location": copy.location,
                            }
                            for copy in record.copies
                        ],
                    }
                )

        results.sort(key=lambda item: (str(item["kind"]), str(item["target"])))
        return results[:limit]


class AcceptancePlanningService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state
        self._iso_service = ImageRootPlanningService(
            image_lookup=self._image_root_record,
            list_lookup=self.list_images,
            plan_lookup=self.get_plan,
        )

    def get_plan(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "fill",
        order: str = "desc",
        q: str | None = None,
        collection: str | None = None,
        iso_ready: bool | None = None,
    ) -> dict[str, object]:
        candidates = [
            image
            for image in self.state.candidates_by_id.values()
            if ImageId(image.finalized_id) not in self.state.finalized_images_by_id
        ]
        covered = {
            (collection_id, path)
            for image in self.state.candidates_by_id.values()
            for collection_id, path in image.covered_paths
        }
        unplanned_bytes = sum(
            record.bytes
            for collection_files in self.state.files_by_collection.values()
            for record in collection_files.values()
            if (record.collection_id, record.path) not in covered
        )
        if q:
            needle = q.casefold()
            candidates = [
                candidate
                for candidate in candidates
                if needle in str(candidate.candidate_id).casefold()
                or any(
                    needle in collection_id.casefold() for collection_id in candidate.collections
                )
                or any(
                    needle in projected_path.casefold()
                    for projected_path in candidate.projected_paths
                )
            ]
        if collection:
            candidates = [
                candidate for candidate in candidates if collection in candidate.collections
            ]
        if iso_ready is not None:
            candidates = [candidate for candidate in candidates if candidate.iso_ready is iso_ready]

        reverse = order == "desc"
        sort_key = {
            "fill": lambda candidate: (
                candidate.fill,
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "bytes": lambda candidate: (
                candidate.bytes,
                candidate.fill,
                str(candidate.candidate_id),
            ),
            "files": lambda candidate: (
                candidate.files,
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "collections": lambda candidate: (
                len(candidate.collections),
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "candidate_id": lambda candidate: (str(candidate.candidate_id),),
        }[sort]
        candidates = sorted(candidates, key=sort_key, reverse=reverse)

        total = len(candidates)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        stop = start + per_page
        page_candidates = candidates[start:stop]
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "ready": bool(
                [
                    image
                    for image in self.state.candidates_by_id.values()
                    if ImageId(image.finalized_id) not in self.state.finalized_images_by_id
                ]
            ),
            "target_bytes": TARGET_BYTES,
            "min_fill_bytes": MIN_FILL_BYTES,
            "candidates": [candidate.plan_payload() for candidate in page_candidates],
            "unplanned_bytes": unplanned_bytes,
        }

    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_copies: bool | None,
    ) -> dict[str, object]:
        images = list(self.state.finalized_images_by_id.values())
        if q:
            needle = q.casefold()
            images = [
                image
                for image in images
                if needle in image.finalized_id.casefold()
                or needle in image.filename.casefold()
                or any(needle in collection_id.casefold() for collection_id in image.collections)
            ]
        if collection:
            images = [image for image in images if collection in image.collections]
        if has_copies is not None:
            images = [image for image in images if (self._copy_count(image) > 0) is has_copies]

        reverse = order == "desc"
        sort_key = {
            "finalized_at": lambda image: (image.finalized_id, image.filename),
            "bytes": lambda image: (image.bytes, image.finalized_id),
            "copy_count": lambda image: (self._copy_count(image), image.finalized_id),
        }[sort]
        images = sorted(images, key=sort_key, reverse=reverse)

        total = len(images)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        stop = start + per_page
        page_images = images[start:stop]
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "images": [
                image.finalized_image_payload(copy_count=self._copy_count(image))
                for image in page_images
            ],
        }

    def get_image(self, image_id: str) -> dict[str, object]:
        image = self._finalized_image_record(image_id)
        return image.finalized_image_payload(copy_count=self._copy_count(image))

    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        candidate = self._candidate_record(candidate_id)
        if not candidate.iso_ready:
            raise InvalidState("image must be ISO-ready before finalization")
        finalized_key = ImageId(candidate.finalized_id)
        self.state.finalized_images_by_id.setdefault(finalized_key, candidate)
        image = self.state.finalized_images_by_id[finalized_key]
        return image.finalized_image_payload(copy_count=self._copy_count(image))

    async def get_iso_stream(self, image_id: str) -> object:
        return await self._iso_service.get_iso_stream(image_id)

    def _candidate_record(self, candidate_id: str) -> CandidateRecord:
        image = self.state.candidates_by_id.get(ImageId(candidate_id))
        if image is None:
            raise NotFound(f"candidate not found: {candidate_id}")
        return image

    def _finalized_image_record(self, image_id: str) -> CandidateRecord:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def _image_root_record(self, image_id: str) -> ImageRootRecord:
        return self._finalized_image_record(image_id).image_root_record()

    def _copy_count(self, image: CandidateRecord) -> int:
        return sum(
            1
            for volume_id, _copy_id in self.state.copy_summaries
            if volume_id == image.finalized_id
        )


class AcceptanceCopyService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        copy_key = CopyId(copy_id)
        scoped_key = (image.finalized_id, copy_key)
        if scoped_key in self.state.copy_summaries:
            raise Conflict(f"copy already exists for volume: {copy_id}")
        summary = CopySummary(
            id=copy_key,
            volume_id=image.finalized_id,
            location=location,
            created_at=DEFAULT_COPY_CREATED_AT,
        )
        self.state.copy_summaries[scoped_key] = summary
        for collection_id, path in image.covered_paths:
            record = self.state.files_by_collection[collection_id][path]
            record.archived = True
            if all(
                (existing.id, existing.volume_id) != (copy_key, image.finalized_id)
                for existing in record.copies
            ):
                record.copies.append(
                    AcceptanceState._copy_from_dict(
                        build_file_copy(
                            copy_id=copy_id,
                            volume_id=image.finalized_id,
                            location=location,
                            collection_id=str(collection_id),
                            path=path,
                        )
                    )
                )
        return summary


class AcceptanceFetchService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def find_reusable_fetch(self, target: TargetStr) -> FetchSummary | None:
        for record in self.state.fetches.values():
            if record.summary.target != target:
                continue
            if record.summary.state == FetchState.FAILED:
                continue
            return record.summary
        return None

    def create_fetch(
        self,
        target: TargetStr,
        files: list[StoredFile],
        *,
        fetch_id: str | None = None,
        initial_state: FetchState = FetchState.WAITING_MEDIA,
    ) -> FetchSummary:
        if fetch_id is None:
            self.state.next_fetch_number += 1
            fetch_id = f"fx-{self.state.next_fetch_number}"
        else:
            self.state.reserve_fetch_id(fetch_id)
        fetch_key = FetchId(fetch_id)
        entries = {
            EntryId(f"e{index}"): FetchEntryRecord(
                id=EntryId(f"e{index}"),
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                content=record.content,
                copies=list(record.copies),
            )
            for index, record in enumerate(sorted(files, key=lambda item: item.path), start=1)
        }
        summary = FetchSummary(
            id=fetch_key,
            target=target,
            state=initial_state,
            files=len(entries),
            bytes=sum(entry.bytes for entry in entries.values()),
            copies=self._summary_copies(entries.values()),
        )
        record = FetchRecord(summary=summary, entries=entries)
        record.summary = self._replace_summary(record, state=initial_state)
        self.state.fetches[fetch_key] = record
        return record.summary

    def find_for_target(self, target: TargetStr) -> FetchSummary:
        summary = self.find_reusable_fetch(target)
        if summary is None:
            raise NotFound(f"fetch not found for target: {target}")
        return summary

    def remove_for_target(self, target: TargetStr) -> None:
        to_delete = [
            fetch_id
            for fetch_id, record in self.state.fetches.items()
            if record.summary.target == target
        ]
        for fetch_id in to_delete:
            del self.state.fetches[fetch_id]

    def get(self, fetch_id: str) -> FetchSummary:
        record = self._record(fetch_id)
        record.summary = self._replace_summary(record)
        return record.summary

    def manifest(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        record.summary = self._replace_summary(record)
        return {
            "id": str(record.summary.id),
            "target": str(record.summary.target),
            "entries": [
                {
                    "id": str(entry.id),
                    "path": entry.path,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "recovery_bytes": self._entry_recovery_bytes(entry),
                    "upload_state": self._entry_upload_state(entry),
                    "uploaded_bytes": entry.uploaded_bytes,
                    "upload_state_expires_at": entry.upload_expires_at,
                    "copies": [self._manifest_copy(entry, copy) for copy in entry.copies],
                    "parts": self._manifest_parts(entry),
                }
                for entry in record.entries.values()
            ],
        }

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if entry.upload_url is None:
            entry.upload_url = (
                f"{FIXTURE_UPLOAD_URL_BASE}/fetches/{record.summary.id}/entries/{entry.id}"
            )
        if self._entry_upload_state(entry) != "uploaded":
            entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        record.summary = self._replace_summary(record)
        return {
            "entry": str(entry.id),
            "protocol": "tus",
            "upload_url": entry.upload_url,
            "offset": entry.uploaded_bytes,
            "length": self._entry_recovery_bytes(entry),
            "checksum_algorithm": "sha256",
            "expires_at": entry.upload_expires_at,
        }

    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        record = self._record(fetch_id)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if offset != entry.uploaded_bytes:
            raise Conflict("upload offset did not match current entry offset")
        algorithm, separator, digest = checksum.partition(" ")
        if separator != " " or algorithm != "sha256":
            raise InvalidState("upload checksum must use sha256")
        actual_digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        if digest != actual_digest:
            raise HashMismatch("upload checksum did not match the provided chunk")
        next_uploaded_bytes = offset + len(content)
        if next_uploaded_bytes > self._entry_recovery_bytes(entry):
            raise Conflict("upload chunk exceeded the expected entry length")

        current_content = entry.uploaded_content or b""
        entry.uploaded_content = current_content + content
        entry.uploaded_bytes = next_uploaded_bytes

        if entry.uploaded_bytes < self._entry_recovery_bytes(entry):
            entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        else:
            self._verify_uploaded_entry(entry)
            entry.upload_expires_at = None

        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

        return {
            "offset": entry.uploaded_bytes,
            "expires_at": entry.upload_expires_at,
        }

    def complete(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        if any(entry.uploaded_content is None for entry in record.entries.values()):
            raise InvalidState("fetch is missing required entry uploads")
        record.summary = self._replace_summary(record, state=FetchState.VERIFYING)
        for entry in record.entries.values():
            self._verify_uploaded_entry(entry)
            stored = self.state.files_by_collection[entry.collection_id][entry.path]
            stored.hot = True
        record.summary = self._replace_summary(record, state=FetchState.DONE)
        hot = self._hot_payload(str(record.summary.target))
        return {
            "id": str(record.summary.id),
            "state": record.summary.state.value,
            "hot": hot,
        }

    def upload_all_required_entries(self, fetch_id: str) -> None:
        record = self._record(fetch_id)
        for entry in record.entries.values():
            recovery_stream = b"".join(self._entry_recovery_payloads(entry))
            entry.uploaded_bytes = len(recovery_stream)
            entry.uploaded_content = recovery_stream
            entry.upload_expires_at = None
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        record = self._record(fetch_id)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        recovery_stream = b"".join(self._entry_recovery_payloads(entry))
        partial = recovery_stream[: max(1, len(recovery_stream) // 2)]
        entry.uploaded_bytes = len(partial)
        entry.uploaded_content = partial
        entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)
        return len(partial)

    def _record(self, fetch_id: str) -> FetchRecord:
        try:
            return self.state.fetches[FetchId(fetch_id)]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

    def _replace_summary(
        self, record: FetchRecord, *, state: FetchState | None = None
    ) -> FetchSummary:
        summary = record.summary
        entries = list(record.entries.values())
        entries_total = len(entries)
        entries_pending = sum(
            1 for entry in entries if self._entry_upload_state(entry) == "pending"
        )
        entries_partial = sum(
            1 for entry in entries if self._entry_upload_state(entry) == "partial"
        )
        entries_uploaded = sum(
            1 for entry in entries if self._entry_upload_state(entry) == "uploaded"
        )
        uploaded_bytes = sum(entry.uploaded_bytes for entry in entries)
        missing_bytes = max(
            sum(self._entry_recovery_bytes(entry) for entry in entries) - uploaded_bytes, 0
        )
        upload_expiries = [
            entry.upload_expires_at for entry in entries if entry.upload_expires_at is not None
        ]
        return FetchSummary(
            id=summary.id,
            target=summary.target,
            state=state or summary.state,
            files=summary.files,
            bytes=summary.bytes,
            copies=list(summary.copies),
            entries_total=entries_total,
            entries_pending=entries_pending,
            entries_partial=entries_partial,
            entries_uploaded=entries_uploaded,
            uploaded_bytes=uploaded_bytes,
            missing_bytes=missing_bytes,
            upload_state_expires_at=max(upload_expiries) if upload_expiries else None,
        )

    def _entry_upload_state(self, entry: FetchEntryRecord) -> str:
        if (
            entry.uploaded_content is not None
            and entry.uploaded_bytes >= self._entry_recovery_bytes(entry)
        ):
            return "uploaded"
        if entry.uploaded_bytes > 0:
            return "partial"
        return "pending"

    @staticmethod
    def _summary_copies(entries: Iterator[FetchEntryRecord]) -> list[FetchCopyHint]:
        out: list[FetchCopyHint] = []
        seen: set[tuple[str, CopyId]] = set()
        for entry in entries:
            for copy in entry.copies:
                key = (copy.volume_id, copy.id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(copy.hint)
        return out

    def _manifest_copy(self, entry: FetchEntryRecord, copy: FileCopy) -> dict[str, object]:
        recovery_payload = self._copy_recovery_payload(entry, copy)
        return {
            "copy": str(copy.id),
            "volume_id": copy.volume_id,
            "location": copy.location,
            "disc_path": copy.disc_path,
            "recovery_bytes": len(recovery_payload),
            "recovery_sha256": hashlib.sha256(recovery_payload).hexdigest(),
        }

    def _manifest_parts(self, entry: FetchEntryRecord) -> list[dict[str, object]]:
        if not entry.copies:
            return []

        if all(copy.part_index is None for copy in entry.copies):
            return [
                {
                    "index": 0,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "recovery_bytes": self._entry_recovery_bytes(entry),
                    "copies": [self._manifest_copy(entry, copy) for copy in entry.copies],
                }
            ]

        part_count = max((copy.part_count or 1) for copy in entry.copies)
        parts: list[dict[str, object]] = []
        for part_index in range(part_count):
            part_copies = [copy for copy in entry.copies if copy.part_index == part_index]
            if not part_copies:
                raise NotFound(f"missing copy hints for part {part_index} of entry {entry.id}")
            bytes_hint = part_copies[0].part_bytes
            sha256_hint = part_copies[0].part_sha256
            if bytes_hint is None or sha256_hint is None:
                raise NotFound(f"missing part metadata for part {part_index} of entry {entry.id}")
            parts.append(
                {
                    "index": part_index,
                    "bytes": bytes_hint,
                    "sha256": sha256_hint,
                    "recovery_bytes": len(self._copy_recovery_payload(entry, part_copies[0])),
                    "copies": [self._manifest_copy(entry, copy) for copy in part_copies],
                }
            )
        return parts

    def _entry_recovery_payloads(self, entry: FetchEntryRecord) -> tuple[bytes, ...]:
        if not entry.copies or all(copy.part_index is None for copy in entry.copies):
            return (fixture_encrypt_bytes(entry.content),)
        part_count = max((copy.part_count or 1) for copy in entry.copies)
        return tuple(
            fixture_encrypt_bytes(part)
            for part in split_fixture_plaintext(entry.content, part_count)
        )

    def _entry_recovery_bytes(self, entry: FetchEntryRecord) -> int:
        return sum(len(payload) for payload in self._entry_recovery_payloads(entry))

    def _copy_recovery_payload(self, entry: FetchEntryRecord, copy: FileCopy) -> bytes:
        payloads = self._entry_recovery_payloads(entry)
        if copy.part_index is None:
            return payloads[0]
        return payloads[copy.part_index]

    def _verify_uploaded_entry(self, entry: FetchEntryRecord) -> None:
        if entry.uploaded_content is None:
            raise InvalidState("fetch is missing required entry uploads")
        recovery_payloads = self._entry_recovery_payloads(entry)
        offset = 0
        plaintext_parts: list[bytes] = []
        for recovery_payload in recovery_payloads:
            next_offset = offset + len(recovery_payload)
            chunk = entry.uploaded_content[offset:next_offset]
            if len(chunk) != len(recovery_payload):
                raise HashMismatch(
                    "uploaded recovery stream did not match expected recovery boundaries"
                )
            try:
                plaintext_parts.append(fixture_decrypt_bytes(chunk))
            except ValueError as exc:
                raise HashMismatch("uploaded recovery bytes did not decrypt cleanly") from exc
            offset = next_offset
        if offset != len(entry.uploaded_content):
            raise HashMismatch("uploaded recovery stream contained trailing bytes")
        actual_sha = hashlib.sha256(b"".join(plaintext_parts)).hexdigest()
        if actual_sha != entry.sha256:
            raise HashMismatch("sha256 did not match expected entry hash")

    def _hot_payload(self, raw_target: str) -> dict[str, object]:
        selected = self.state.selected_files(raw_target)
        present_bytes = sum(record.bytes for record in selected if record.hot)
        missing_bytes = sum(record.bytes for record in selected if not record.hot)
        return {
            "state": "ready" if missing_bytes == 0 else "waiting",
            "present_bytes": present_bytes,
            "missing_bytes": missing_bytes,
        }


class AcceptancePinService:
    def __init__(self, state: AcceptanceState, fetches: AcceptanceFetchService) -> None:
        self.state = state
        self.fetches = fetches

    def pin(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = cast(TargetStr, target.canonical)
        selected = self.state.selected_files(target.canonical)
        self.state.exact_pins.add(canonical)

        present_bytes = sum(record.bytes for record in selected if record.hot)
        missing_bytes = sum(record.bytes for record in selected if not record.hot)
        summary = self.fetches.find_reusable_fetch(canonical)
        if summary is None:
            summary = self.fetches.create_fetch(
                canonical,
                selected,
                initial_state=FetchState.DONE if missing_bytes == 0 else FetchState.WAITING_MEDIA,
            )
        fetch_payload = {
            "id": str(summary.id),
            "state": summary.state.value,
            "copies": [
                {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
                for copy in summary.copies
            ],
        }
        return {
            "target": str(canonical),
            "pin": True,
            "hot": {
                "state": "ready" if missing_bytes == 0 else "waiting",
                "present_bytes": present_bytes,
                "missing_bytes": missing_bytes,
            },
            "fetch": fetch_payload,
        }

    def release(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = cast(TargetStr, target.canonical)
        removed = canonical in self.state.exact_pins
        self.state.exact_pins.discard(canonical)
        if removed:
            self.fetches.remove_for_target(canonical)
        self.state.reconcile_hot_from_pins()
        return {
            "target": str(canonical),
            "pin": False,
        }

    def list_pins(self) -> list[PinSummary]:
        return [
            PinSummary(target=target, fetch=self.fetches.find_for_target(target))
            for target in sorted(self.state.exact_pins)
        ]


class _LiveServerHandle:
    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=self.base_url, timeout=0.5) as client:
                    response = client.get("/openapi.json")
                if response.status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(
            f"Timed out waiting for live arc test server at {self.base_url}"
        ) from last_error

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():  # pragma: no cover
            raise RuntimeError("Timed out stopping live arc test server")


@dataclass(slots=True)
class _PortReservation:
    socket: socket.socket
    port: int

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> _PortReservation:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _reserve_local_port() -> _PortReservation:
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen(1)
    return _PortReservation(socket=reserved, port=int(reserved.getsockname()[1]))


@dataclass(slots=True)
class AcceptanceSystem:
    workspace: Path
    state: AcceptanceState
    app: Any
    server: _LiveServerHandle
    base_url: str
    fixture_path: Path
    collections: AcceptanceCollectionService
    search: AcceptanceSearchService
    planning: AcceptancePlanningService
    copies: AcceptanceCopyService
    pins: AcceptancePinService
    fetches: AcceptanceFetchService

    @classmethod
    def create(cls, workspace: Path) -> AcceptanceSystem:
        state = AcceptanceState()
        return cls._create_with_state(workspace, state)

    @classmethod
    def _create_with_state(cls, workspace: Path, state: AcceptanceState) -> AcceptanceSystem:
        collections = AcceptanceCollectionService(state)
        search = AcceptanceSearchService(state)
        planning = AcceptancePlanningService(state)
        copies = AcceptanceCopyService(state)
        fetches = AcceptanceFetchService(state)
        pins = AcceptancePinService(state, fetches)

        app = create_app()
        container = ServiceContainer(
            collections=collections,
            search=search,
            planning=planning,
            copies=copies,
            pins=pins,
            fetches=fetches,
        )
        app.dependency_overrides[get_container] = lambda: container

        fixture_path = workspace / "arc_disc_fixture.json"
        with _reserve_local_port() as reserved:
            server = _LiveServerHandle(app, host="127.0.0.1", port=reserved.port)
        server.start()
        return cls(
            workspace=workspace,
            state=state,
            app=app,
            server=server,
            base_url=server.base_url,
            fixture_path=fixture_path,
            collections=collections,
            search=search,
            planning=planning,
            copies=copies,
            pins=pins,
            fetches=fetches,
        )

    def restart(self) -> None:
        self.app.dependency_overrides.clear()
        self.server.close()
        restarted = self._create_with_state(self.workspace, self.state)
        self.app = restarted.app
        self.server = restarted.server
        self.base_url = restarted.base_url
        self.collections = restarted.collections
        self.search = restarted.search
        self.planning = restarted.planning
        self.copies = restarted.copies
        self.pins = restarted.pins
        self.fetches = restarted.fetches

    def close(self) -> None:
        self.app.dependency_overrides.clear()
        self.server.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
                    return client.request(
                        method,
                        path,
                        params=params,
                        json=json_body,
                        headers=headers,
                        content=content,
                    )
            except httpx.RemoteProtocolError:
                if not path.endswith("/iso") or attempt == 2:
                    raise
                time.sleep(0.05)
        raise RuntimeError("unreachable")

    def seed_finalized_image(self, candidate_id: str, *, force_ready: bool = False) -> None:
        candidate_key = ImageId(candidate_id)
        candidate = self.state.candidates_by_id[candidate_key]
        if force_ready and not candidate.iso_ready:
            candidate = CandidateRecord(
                candidate_id=candidate.candidate_id,
                finalized_id=candidate.finalized_id,
                filename=candidate.filename,
                image_root=candidate.image_root,
                bytes=candidate.bytes,
                iso_ready=True,
                covered_paths=candidate.covered_paths,
            )
            self.state.candidates_by_id[candidate_key] = candidate
        self.state.finalized_images_by_id[ImageId(candidate.finalized_id)] = candidate

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "arc_cli.main", *args],
            cwd=REPO_ROOT,
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_arc_disc(
        self, *args: str, input_text: str = "\n" * 16
    ) -> subprocess.CompletedProcess[str]:
        if not self.fixture_path.exists():
            self.configure_arc_disc_fixture()
        env = self._subprocess_env(
            {
                "ARC_DISC_FIXTURE_PATH": str(self.fixture_path),
                "ARC_DISC_READER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureOpticalReader",
            }
        )
        return subprocess.run(
            [sys.executable, "-m", "arc_disc.main", *args],
            cwd=REPO_ROOT,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_staged_collection(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        root = write_tree(
            self.workspace / "staging" / normalized_collection_id, files or PHOTOS_2024_FILES
        )
        self.state.register_staged_directory(
            staging_path_for_collection(normalized_collection_id), root
        )

    def seed_staged_photos(self) -> None:
        self.seed_staged_collection(PHOTOS_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_photos_hot(self) -> None:
        self.state.seed_collection(
            PHOTOS_COLLECTION_ID,
            PHOTOS_2024_FILES,
            hot_paths=set(PHOTOS_2024_FILES),
            archived_paths=set(),
        )

    def seed_nested_photos_hot(self) -> None:
        self.state.seed_collection(
            PHOTOS_NESTED_COLLECTION_ID,
            PHOTOS_2024_FILES,
            hot_paths=set(PHOTOS_2024_FILES),
            archived_paths=set(),
        )

    def seed_parent_photos_hot(self) -> None:
        self.state.seed_collection(
            PHOTOS_PARENT_COLLECTION_ID,
            PHOTOS_2024_FILES,
            hot_paths=set(PHOTOS_2024_FILES),
            archived_paths=set(),
        )

    def seed_docs_hot(self) -> None:
        self.state.seed_collection(
            DOCS_COLLECTION_ID,
            DOCS_FILES,
            hot_paths=set(DOCS_FILES),
            archived_paths=set(),
        )

    def seed_docs_archive(self) -> None:
        self.state.seed_collection(
            DOCS_COLLECTION_ID,
            DOCS_FILES,
            hot_paths={"tax/2022/receipt-456.pdf", "letters/cover.txt"},
            archived_paths={"tax/2022/invoice-123.pdf", "tax/2022/receipt-456.pdf"},
            copies_by_path={
                "tax/2022/invoice-123.pdf": [
                    build_file_copy(
                        copy_id="copy-docs-1",
                        volume_id="20260419T230001Z",
                        location="vault-a/shelf-01",
                        collection_id=DOCS_COLLECTION_ID,
                        path="tax/2022/invoice-123.pdf",
                    )
                ],
                "tax/2022/receipt-456.pdf": [
                    build_file_copy(
                        copy_id="copy-docs-2",
                        volume_id="20260419T230002Z",
                        location="vault-a/shelf-02",
                        collection_id=DOCS_COLLECTION_ID,
                        path="tax/2022/receipt-456.pdf",
                    )
                ],
            },
        )

    def seed_docs_archive_with_split_invoice(self) -> None:
        self.state.seed_collection(
            DOCS_COLLECTION_ID,
            DOCS_FILES,
            hot_paths={"tax/2022/receipt-456.pdf", "letters/cover.txt"},
            archived_paths={"tax/2022/invoice-123.pdf", "tax/2022/receipt-456.pdf"},
            copies_by_path={
                "tax/2022/invoice-123.pdf": [
                    build_file_copy(
                        copy_id=SPLIT_COPY_ONE_ID,
                        volume_id="20260420T040003Z",
                        location=SPLIT_COPY_ONE_LOCATION,
                        collection_id=DOCS_COLLECTION_ID,
                        path=SPLIT_FILE_RELPATH,
                        part_index=0,
                        part_count=len(SPLIT_FILE_PARTS),
                        part_bytes=len(SPLIT_FILE_PARTS[0]),
                        part_sha256=hashlib.sha256(SPLIT_FILE_PARTS[0]).hexdigest(),
                    ),
                    build_file_copy(
                        copy_id=SPLIT_COPY_TWO_ID,
                        volume_id="20260420T040004Z",
                        location=SPLIT_COPY_TWO_LOCATION,
                        collection_id=DOCS_COLLECTION_ID,
                        path=SPLIT_FILE_RELPATH,
                        part_index=1,
                        part_count=len(SPLIT_FILE_PARTS),
                        part_bytes=len(SPLIT_FILE_PARTS[1]),
                        part_sha256=hashlib.sha256(SPLIT_FILE_PARTS[1]).hexdigest(),
                    ),
                ],
                "tax/2022/receipt-456.pdf": [
                    build_file_copy(
                        copy_id="copy-docs-2",
                        volume_id="20260419T230002Z",
                        location="vault-a/shelf-02",
                        collection_id=DOCS_COLLECTION_ID,
                        path="tax/2022/receipt-456.pdf",
                    )
                ],
            },
        )

    def seed_search_fixtures(self) -> None:
        self.seed_docs_archive()
        self.seed_photos_hot()

    def seed_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_photos_hot()
        self.seed_image_fixtures(IMAGE_FIXTURES)

    def seed_split_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)

    def seed_image_fixtures(self, fixtures: tuple[Any, ...]) -> None:
        images_root = self.workspace / "images"
        for fixture in fixtures:
            image_root = write_tree(images_root / fixture.id, fixture.files)
            self.state.seed_image(
                CandidateRecord(
                    candidate_id=ImageId(fixture.id),
                    finalized_id=fixture.volume_id,
                    filename=fixture.filename,
                    image_root=image_root,
                    bytes=fixture.bytes,
                    iso_ready=fixture.iso_ready,
                    covered_paths=tuple(
                        (CollectionId(collection_id), path)
                        for collection_id, path in fixture.covered_paths
                    ),
                )
            )

    def seed_pin(self, raw_target: str) -> None:
        self.pins.pin(raw_target)

    def seed_fetch(self, fetch_id: str, raw_target: str) -> None:
        canonical = cast(TargetStr, parse_target(raw_target).canonical)
        self.fetches.remove_for_target(canonical)
        files = self.state.selected_files(raw_target)
        self.fetches.create_fetch(canonical, files, fetch_id=fetch_id)

    def seed_api_registered_split_archive(self, fetch_id: str, target: str) -> None:
        target_path = parse_target(target).path
        collection_id = target_path.parts[0]
        file_path = str(target_path.relative_to(collection_id))

        covering_fixtures = [
            f for f in SPLIT_IMAGE_FIXTURES
            if any(coll == collection_id and p == file_path for coll, p in f.covered_paths)
        ]
        assert covering_fixtures, f"no SPLIT_IMAGE_FIXTURES cover {collection_id}/{file_path}"

        self.seed_split_planner_fixtures()

        for i, fixture in enumerate(covering_fixtures, start=1):
            resp = self.request("POST", f"/v1/plan/candidates/{fixture.id}/finalize")
            assert resp.status_code == 200, resp.text
            image_id = resp.json()["id"]

            resp = self.request(
                "POST",
                f"/v1/images/{image_id}/copies",
                json_body={"id": f"api-split-copy-{i}", "location": f"vault-api/shelf-{i:02d}"},
            )
            assert resp.status_code == 200, resp.text

        self.state.files_by_collection[CollectionId(collection_id)][file_path].hot = False

        self.seed_fetch(fetch_id, target)

    def upload_required_entries(self, fetch_id: str) -> None:
        self.fetches.upload_all_required_entries(fetch_id)

    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        return self.fetches.upload_partial_entry(fetch_id, entry_id)

    def upload_buffer_absent(self, fetch_id: str) -> bool:
        return True  # stub has no real filesystem upload buffers

    def pins_list(self) -> list[str]:
        return [str(item.target) for item in self.pins.list_pins()]

    def uploaded_entry_content(self, fetch_id: str, entry_path: str) -> bytes | None:
        record = self.state.fetches[FetchId(fetch_id)]
        for entry in record.entries.values():
            if entry.path == entry_path:
                return entry.uploaded_content
        raise NotFound(f"entry not found for {fetch_id}: {entry_path}")

    def configure_arc_disc_fixture(
        self,
        *,
        fetch_id: str = "fx-1",
        fail_path: str | None = None,
        corrupt_path: str | None = None,
        fail_copy_ids: set[str] | None = None,
        corrupt_copy_ids: set[str] | None = None,
    ) -> None:
        manifest = cast(dict[str, Any], self.fetches.manifest(fetch_id))
        fetch_record = self.state.fetches[FetchId(fetch_id)]
        content_by_path = {entry.path: entry.content for entry in fetch_record.entries.values()}
        payload_by_disc_path: dict[str, str] = {}
        fail_disc_paths: list[str] = []
        fail_copy_ids = fail_copy_ids or set()
        corrupt_copy_ids = corrupt_copy_ids or set()

        for entry in cast(list[dict[str, Any]], manifest["entries"]):
            entry_path = str(entry["path"])
            parts = cast(list[dict[str, Any]], entry["parts"])
            plaintext_parts = split_fixture_plaintext(content_by_path[entry_path], len(parts))
            for part in parts:
                part_index = int(part["index"])
                part_plaintext = plaintext_parts[part_index]
                for copy_info in cast(list[dict[str, Any]], part["copies"]):
                    copy_id = str(copy_info["copy"])
                    disc_path = str(copy_info["disc_path"])
                    payload = fixture_encrypt_bytes(part_plaintext)
                    if entry_path == corrupt_path or copy_id in corrupt_copy_ids:
                        payload = payload + b"corrupted-by-fixture\n"
                    payload_by_disc_path[disc_path] = base64.b64encode(payload).decode("ascii")
                    if entry_path == fail_path or copy_id in fail_copy_ids:
                        fail_disc_paths.append(disc_path)

        payload = {
            "reader": {
                "payload_by_disc_path": payload_by_disc_path,
                "fail_disc_paths": fail_disc_paths,
            },
        }
        self.fixture_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        if extra:
            env.update(extra)
        return env


@pytest.fixture
def acceptance_system(tmp_path: Path) -> Iterator[AcceptanceSystem]:
    system = AcceptanceSystem.create(tmp_path)
    try:
        yield system
    finally:
        system.close()
