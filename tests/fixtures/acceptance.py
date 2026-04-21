from __future__ import annotations

import base64
import hashlib
import json
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
from arc_core.services.planning import ImageRootPlanningService, ImageRootRecord
from tests.fixtures.data import (
    DEFAULT_COPY_CREATED_AT,
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    MIN_FILL_BYTES,
    PHOTOS_2024_FILES,
    PHOTOS_COLLECTION_ID,
    SPLIT_COPY_ONE_ID,
    SPLIT_COPY_ONE_LOCATION,
    SPLIT_COPY_TWO_ID,
    SPLIT_COPY_TWO_LOCATION,
    SPLIT_FILE_PARTS,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    STAGING_PATH,
    TARGET_BYTES,
    build_file_copy,
    split_fixture_plaintext,
    write_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


@dataclass(frozen=True, slots=True)
class FileCopy:
    id: CopyId
    location: str
    disc_path: str
    enc: dict[str, object]
    part_index: int | None = None
    part_count: int | None = None
    part_bytes: int | None = None
    part_sha256: str | None = None

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, location=self.location)


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
    def canonical_target(self) -> str:
        return f"{self.collection_id}:/{self.path}"


@dataclass(frozen=True, slots=True)
class ImageRecord:
    id: ImageId
    volume_id: str
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
    def fill(self) -> float:
        return self.bytes / TARGET_BYTES

    def plan_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "bytes": self.bytes,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "iso_ready": self.iso_ready,
        }

    def image_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "bytes": self.bytes,
            "fill": self.fill,
            "files": self.files,
            "collections": self.collections,
            "iso_ready": self.iso_ready,
        }

    def image_root_record(self) -> ImageRootRecord:
        return ImageRootRecord(
            image_id=str(self.id),
            volume_id=self.volume_id,
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
    uploaded_content: bytes | None = None


@dataclass(slots=True)
class FetchRecord:
    summary: FetchSummary
    entries: dict[EntryId, FetchEntryRecord]


@dataclass(slots=True)
class AcceptanceState:
    staged_directories: dict[str, Path] = field(default_factory=dict)
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    images_by_id: dict[ImageId, ImageRecord] = field(default_factory=dict)
    copy_summaries: dict[CopyId, CopySummary] = field(default_factory=dict)
    exact_pins: set[TargetStr] = field(default_factory=set)
    fetches: dict[FetchId, FetchRecord] = field(default_factory=dict)
    rejected_upload_codes: list[str] = field(default_factory=list)
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
        collection_key = CollectionId(collection_id)
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

    def seed_image(self, image: ImageRecord) -> None:
        self.images_by_id[image.id] = image

    def collection_files(self, collection_id: str | CollectionId) -> list[StoredFile]:
        collection_key = CollectionId(str(collection_id))
        records = self.files_by_collection.get(collection_key)
        if records is None:
            raise NotFound(f"collection not found: {collection_key}")
        return list(records.values())

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
        records = self.files_by_collection.get(target.collection_id)
        if records is None:
            if missing_ok:
                return []
            raise NotFound(f"collection not found: {target.collection_id}")
        if target.is_collection:
            selected = list(records.values())
        else:
            assert target.path is not None
            logical_path = str(target.path).lstrip("/")
            if target.is_dir:
                prefix = logical_path.rstrip("/") + "/"
                selected = [record for record in records.values() if record.path.startswith(prefix)]
            else:
                record = records.get(logical_path)
                selected = [] if record is None else [record]
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
        collection_id = root.name
        if CollectionId(collection_id) in self.state.files_by_collection:
            raise Conflict(f"collection already exists: {collection_id}")
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
                        "target": collection_name,
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
            for record in sorted(self.state.collection_files(collection_id), key=lambda item: item.path):
                full_path = f"/{record.path}"
                if needle not in full_path.casefold():
                    continue
                results.append(
                    {
                        "kind": "file",
                        "target": record.canonical_target,
                        "collection": collection_name,
                        "path": full_path,
                        "bytes": record.bytes,
                        "hot": record.hot,
                        "copies": [{"id": str(copy.id), "location": copy.location} for copy in record.copies],
                    }
                )

        results.sort(key=lambda item: (str(item["kind"]), str(item["target"])))
        return results[:limit]


class AcceptancePlanningService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state
        self._iso_service = ImageRootPlanningService(
            image_lookup=self._image_root_record,
            plan_lookup=self.get_plan,
        )

    def get_plan(self) -> dict[str, object]:
        images = sorted(self.state.images_by_id.values(), key=lambda image: (-image.fill, str(image.id)))
        covered = {
            (collection_id, path)
            for image in self.state.images_by_id.values()
            for collection_id, path in image.covered_paths
        }
        unplanned_bytes = sum(
            record.bytes
            for collection_files in self.state.files_by_collection.values()
            for record in collection_files.values()
            if (record.collection_id, record.path) not in covered
        )
        return {
            "ready": bool(images),
            "target_bytes": TARGET_BYTES,
            "min_fill_bytes": MIN_FILL_BYTES,
            "images": [image.plan_payload() for image in images],
            "unplanned_bytes": unplanned_bytes,
        }

    def get_image(self, image_id: str) -> dict[str, object]:
        return self._image_record(image_id).image_payload()

    async def get_iso_stream(self, image_id: str) -> object:
        return await self._iso_service.get_iso_stream(image_id)

    def _image_record(self, image_id: str) -> ImageRecord:
        image = self.state.images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def _image_root_record(self, image_id: str) -> ImageRootRecord:
        return self._image_record(image_id).image_root_record()


class AcceptanceCopyService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        copy_key = CopyId(copy_id)
        if copy_key in self.state.copy_summaries:
            raise Conflict(f"copy already exists: {copy_id}")
        image = self.state.images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        summary = CopySummary(
            id=copy_key,
            image=ImageId(image_id),
            location=location,
            created_at=DEFAULT_COPY_CREATED_AT,
        )
        self.state.copy_summaries[copy_key] = summary
        for collection_id, path in image.covered_paths:
            record = self.state.files_by_collection[collection_id][path]
            record.archived = True
            if all(existing.id != copy_key for existing in record.copies):
                record.copies.append(
                    AcceptanceState._copy_from_dict(
                        build_file_copy(
                            copy_id=copy_id,
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
            if record.summary.state in {FetchState.DONE, FetchState.FAILED}:
                continue
            return record.summary
        return None

    def create_fetch(self, target: TargetStr, files: list[StoredFile], *, fetch_id: str | None = None) -> FetchSummary:
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
            state=FetchState.WAITING_MEDIA,
            files=len(entries),
            bytes=sum(entry.bytes for entry in entries.values()),
            copies=self._summary_copies(entries.values()),
        )
        self.state.fetches[fetch_key] = FetchRecord(summary=summary, entries=entries)
        return summary

    def get(self, fetch_id: str) -> FetchSummary:
        return self._record(fetch_id).summary

    def manifest(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        return {
            "id": str(record.summary.id),
            "target": str(record.summary.target),
            "entries": [
                {
                    "id": str(entry.id),
                    "path": entry.path,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "copies": [self._manifest_copy(copy) for copy in entry.copies],
                    "parts": self._manifest_parts(entry),
                }
                for entry in record.entries.values()
            ],
        }

    def upload_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes) -> dict[str, object]:
        record = self._record(fetch_id)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        actual_sha = hashlib.sha256(content).hexdigest()
        if sha256 != entry.sha256 or actual_sha != entry.sha256:
            self.state.rejected_upload_codes.append("hash_mismatch")
            raise HashMismatch("sha256 did not match expected entry hash")
        entry.uploaded_content = content
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_state(record.summary, FetchState.UPLOADING)
        return {
            "entry": str(entry.id),
            "accepted": True,
            "bytes": len(content),
        }

    def complete(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        if any(entry.uploaded_content is None for entry in record.entries.values()):
            raise InvalidState("fetch is missing required entry uploads")
        record.summary = self._replace_state(record.summary, FetchState.VERIFYING)
        for entry in record.entries.values():
            stored = self.state.files_by_collection[entry.collection_id][entry.path]
            stored.hot = True
        record.summary = self._replace_state(record.summary, FetchState.DONE)
        hot = self._hot_payload(str(record.summary.target))
        return {
            "id": str(record.summary.id),
            "state": record.summary.state.value,
            "hot": hot,
        }

    def upload_all_required_entries(self, fetch_id: str) -> None:
        record = self._record(fetch_id)
        for entry in record.entries.values():
            entry.uploaded_content = entry.content
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_state(record.summary, FetchState.UPLOADING)

    def _record(self, fetch_id: str) -> FetchRecord:
        try:
            return self.state.fetches[FetchId(fetch_id)]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

    @staticmethod
    def _replace_state(summary: FetchSummary, state: FetchState) -> FetchSummary:
        return FetchSummary(
            id=summary.id,
            target=summary.target,
            state=state,
            files=summary.files,
            bytes=summary.bytes,
            copies=list(summary.copies),
        )

    @staticmethod
    def _summary_copies(entries: Iterator[FetchEntryRecord]) -> list[FetchCopyHint]:
        out: list[FetchCopyHint] = []
        seen: set[CopyId] = set()
        for entry in entries:
            for copy in entry.copies:
                if copy.id in seen:
                    continue
                seen.add(copy.id)
                out.append(copy.hint)
        return out

    @staticmethod
    def _manifest_copy(copy: FileCopy) -> dict[str, object]:
        return {
            "copy": str(copy.id),
            "location": copy.location,
            "disc_path": copy.disc_path,
            "enc": copy.enc,
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
                    "copies": [self._manifest_copy(copy) for copy in entry.copies],
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
                    "copies": [self._manifest_copy(copy) for copy in part_copies],
                }
            )
        return parts

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
        missing = [record for record in selected if not record.hot]
        missing_bytes = sum(record.bytes for record in missing)
        fetch_payload: dict[str, object] | None = None
        if missing:
            summary = self.fetches.find_reusable_fetch(canonical)
            if summary is None:
                summary = self.fetches.create_fetch(canonical, missing)
            fetch_payload = {
                "id": str(summary.id),
                "state": summary.state.value,
                "copies": [{"id": str(copy.id), "location": copy.location} for copy in summary.copies],
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
        self.state.reconcile_hot_from_pins()
        return {
            "target": str(canonical),
            "pin": removed,
        }

    def list_pins(self) -> list[PinSummary]:
        return [PinSummary(target=target) for target in sorted(self.state.exact_pins)]


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
        raise RuntimeError(f"Timed out waiting for live arc test server at {self.base_url}") from last_error

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

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "arc_cli.main", *args],
            cwd=REPO_ROOT,
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_arc_disc(self, *args: str, input_text: str = "\n" * 16) -> subprocess.CompletedProcess[str]:
        if not self.fixture_path.exists():
            self.configure_arc_disc_fixture()
        env = self._subprocess_env(
            {
                "ARC_DISC_FIXTURE_PATH": str(self.fixture_path),
                "ARC_DISC_READER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureOpticalReader",
                "ARC_DISC_CRYPTO_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureCrypto",
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

    def seed_staged_photos(self) -> None:
        root = write_tree(self.workspace / "staging" / PHOTOS_COLLECTION_ID, PHOTOS_2024_FILES)
        self.state.register_staged_directory(STAGING_PATH, root)

    def seed_photos_hot(self) -> None:
        self.state.seed_collection(
            PHOTOS_COLLECTION_ID,
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
                        location="vault-a/shelf-01",
                        collection_id=DOCS_COLLECTION_ID,
                        path="tax/2022/invoice-123.pdf",
                    )
                ],
                "tax/2022/receipt-456.pdf": [
                    build_file_copy(
                        copy_id="copy-docs-2",
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
                ImageRecord(
                    id=ImageId(fixture.id),
                    volume_id=fixture.volume_id,
                    filename=fixture.filename,
                    image_root=image_root,
                    bytes=fixture.bytes,
                    iso_ready=fixture.iso_ready,
                    covered_paths=tuple(
                        (CollectionId(collection_id), path) for collection_id, path in fixture.covered_paths
                    ),
                )
            )

    def seed_pin(self, raw_target: str) -> None:
        canonical = cast(TargetStr, parse_target(raw_target).canonical)
        self.state.exact_pins.add(canonical)

    def seed_fetch(self, fetch_id: str, raw_target: str) -> None:
        canonical = cast(TargetStr, parse_target(raw_target).canonical)
        files = self.state.selected_files(raw_target)
        self.fetches.create_fetch(canonical, files, fetch_id=fetch_id)

    def upload_required_entries(self, fetch_id: str) -> None:
        self.fetches.upload_all_required_entries(fetch_id)

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
        files_by_path = {
            record.path: record.content for record in self.state.selected_files(str(manifest["target"]))
        }
        encrypted_by_disc_path: dict[str, str] = {}
        plaintext_by_fixture_key: dict[str, str] = {}
        fail_disc_paths: list[str] = []
        fail_copy_ids = fail_copy_ids or set()
        corrupt_copy_ids = corrupt_copy_ids or set()

        for entry in cast(list[dict[str, Any]], manifest["entries"]):
            entry_path = str(entry["path"])
            parts = cast(list[dict[str, Any]], entry["parts"])
            plaintext_parts = split_fixture_plaintext(files_by_path[entry_path], len(parts))
            for part in parts:
                part_index = int(part["index"])
                part_plaintext = plaintext_parts[part_index]
                for copy_info in cast(list[dict[str, Any]], part["copies"]):
                    copy_id = str(copy_info["copy"])
                    disc_path = str(copy_info["disc_path"])
                    fixture_key = str(copy_info["enc"]["fixture_key"])
                    plaintext = part_plaintext
                    if entry_path == corrupt_path or copy_id in corrupt_copy_ids:
                        plaintext = plaintext + b"corrupted-by-fixture\n"
                    encrypted = f"ciphertext::{fixture_key}".encode()
                    encrypted_by_disc_path[disc_path] = base64.b64encode(encrypted).decode("ascii")
                    plaintext_by_fixture_key[fixture_key] = base64.b64encode(plaintext).decode("ascii")
                    if entry_path == fail_path or copy_id in fail_copy_ids:
                        fail_disc_paths.append(disc_path)

        payload = {
            "reader": {
                "encrypted_by_disc_path": encrypted_by_disc_path,
                "fail_disc_paths": fail_disc_paths,
            },
            "crypto": {
                "plaintext_by_fixture_key": plaintext_by_fixture_key,
            },
        }
        self.fixture_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

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
