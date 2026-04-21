from __future__ import annotations

import hashlib
import importlib
import inspect
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from arc_api.app import create_app
from arc_api.deps import ServiceContainer, get_container
from arc_core.domain.errors import Conflict, NotFound
from arc_core.domain.models import CollectionSummary, CopySummary, FetchCopyHint, FileRef
from arc_core.domain.types import CollectionId, CopyId, ImageId, Sha256Hex
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import ImageRootPlanningService, ImageRootRecord, StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/api.plan_and_images.feature"
IMAGE_ID = "img_2026-04-20_01"
SECOND_IMAGE_ID = "img_2026-04-20_02"
DOCS_COLLECTION_ID = "docs"
TARGET_BYTES = 10_000
MIN_FILL_BYTES = 7_500


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool = True
    archived: bool = False

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        digest = hashlib.sha256(self.content).hexdigest()
        return cast(Sha256Hex, digest)


@dataclass(frozen=True, slots=True)
class PlannerImageFixture:
    id: ImageId
    volume_id: str
    filename: str
    image_root: Path
    bytes: int
    files: int
    collections: list[str]
    iso_ready: bool
    covered_files: list[tuple[CollectionId, str]]

    def summary_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "bytes": self.bytes,
            "fill": self.bytes / TARGET_BYTES,
            "files": self.files,
            "collections": list(self.collections),
            "iso_ready": self.iso_ready,
        }

    def plan_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "bytes": self.bytes,
            "fill": self.bytes / TARGET_BYTES,
            "files": self.files,
            "collections": len(self.collections),
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
class AcceptanceState:
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    images_by_id: dict[ImageId, PlannerImageFixture] = field(default_factory=dict)
    copies_by_id: dict[CopyId, CopySummary] = field(default_factory=dict)

    def seed_collection_from_tree(
        self,
        collection_id: str,
        root: Path,
        *,
        fully_hot: bool,
        fully_archived: bool,
    ) -> None:
        cid = CollectionId(collection_id)
        collection_files: dict[str, StoredFile] = {}
        for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
            relative_path = file_path.relative_to(root).as_posix()
            collection_files[relative_path] = StoredFile(
                collection_id=cid,
                path=relative_path,
                content=file_path.read_bytes(),
                hot=fully_hot,
                archived=fully_archived,
            )
        self.files_by_collection[cid] = collection_files

    def collection_files(self, collection_id: CollectionId) -> list[StoredFile]:
        files = self.files_by_collection.get(collection_id)
        if files is None:
            raise NotFound(f"collection not found: {collection_id}")
        return list(files.values())

    def file_record(self, collection_id: CollectionId, path: str) -> StoredFile:
        files = self.files_by_collection.get(collection_id)
        if files is None or path not in files:
            raise NotFound(f"file not found: {collection_id}:/{path}")
        return files[path]

    def archive_image_coverage(self, image_id: str) -> None:
        image = self.images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        for collection_id, path in image.covered_files:
            self.file_record(collection_id, path).archived = True


@dataclass(slots=True)
class AcceptanceCatalogRepo:
    state: AcceptanceState

    def collection_exists(self, collection_id: CollectionId) -> bool:
        return collection_id in self.state.files_by_collection

    def create_collection_from_scan(self, collection_id: CollectionId, staging_path: str) -> CollectionSummary:
        raise AssertionError(
            f"create_collection_from_scan should not be called by {FEATURE_PATH}: {collection_id=} {staging_path=}"
        )

    def get_collection_summary(self, collection_id: CollectionId) -> CollectionSummary:
        files = self.state.collection_files(collection_id)
        total_bytes = sum(record.bytes for record in files)
        hot_bytes = sum(record.bytes for record in files if record.hot)
        archived_bytes = sum(record.bytes for record in files if record.archived)
        return CollectionSummary(
            id=collection_id,
            files=len(files),
            bytes=total_bytes,
            hot_bytes=hot_bytes,
            archived_bytes=archived_bytes,
        )

    def search(self, query: str, limit: int) -> list[object]:
        raise AssertionError(f"search should not be called by {FEATURE_PATH}: {query=} {limit=}")

    def resolve_target_files(self, target: object) -> list[FileRef]:
        raise AssertionError(f"resolve_target_files should not be called by {FEATURE_PATH}: {target=}")


class AcceptanceCollectionService:
    def __init__(self, *, catalog: AcceptanceCatalogRepo) -> None:
        self.catalog = catalog

    def close(self, staging_path: str) -> object:
        raise AssertionError(f"close should not be called by {FEATURE_PATH}: {staging_path=}")

    def get(self, collection_id: str) -> CollectionSummary:
        return self.catalog.get_collection_summary(CollectionId(collection_id))


@dataclass(slots=True)
class AcceptancePlannerStore:
    state: AcceptanceState

    def has_candidate_image(self) -> bool:
        return bool(self.state.images_by_id)

    def image_exists(self, image_id: str) -> bool:
        return ImageId(image_id) in self.state.images_by_id

    def image_fixture(self, image_id: str) -> PlannerImageFixture:
        image = self.state.images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def seed_image(self, image: PlannerImageFixture) -> None:
        self.state.images_by_id[image.id] = image

    def get_plan_payload(self) -> dict[str, object]:
        images = sorted(
            self.state.images_by_id.values(),
            key=lambda image: (-image.bytes / TARGET_BYTES, str(image.id)),
        )
        covered = {
            (collection_id, path)
            for image in self.state.images_by_id.values()
            for collection_id, path in image.covered_files
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

    def get_image_payload(self, image_id: str) -> dict[str, object]:
        return self.image_fixture(image_id).summary_payload()

    def get_image_root_record(self, image_id: str) -> ImageRootRecord:
        return self.image_fixture(image_id).image_root_record()

    def covered_files_for_image(self, image_id: str) -> list[StoredFile]:
        image = self.image_fixture(image_id)
        return [self.state.file_record(collection_id, path) for collection_id, path in image.covered_files]


@dataclass(slots=True)
class AcceptanceCopyStore:
    state: AcceptanceState
    planner_store: AcceptancePlannerStore
    created_at: str = "2026-04-20T12:00:00Z"

    def copy_exists(self, copy_id: str) -> bool:
        return CopyId(copy_id) in self.state.copies_by_id

    def seed_existing_copy(self, *, image_id: str, copy_id: str, location: str) -> CopySummary:
        summary = self.create_copy(image_id=image_id, copy_id=copy_id, location=location)
        return summary

    def create_copy(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        copy_key = CopyId(copy_id)
        if copy_key in self.state.copies_by_id:
            raise Conflict(f"copy already exists: {copy_id}")
        if not self.planner_store.image_exists(image_id):
            raise NotFound(f"image not found: {image_id}")
        summary = CopySummary(
            id=copy_key,
            image=ImageId(image_id),
            location=location,
            created_at=self.created_at,
        )
        self.state.copies_by_id[copy_key] = summary
        self.state.archive_image_coverage(image_id)
        return summary

    def file_copies(self, collection_id: str, path: str) -> list[FetchCopyHint]:
        collection_key = CollectionId(collection_id)
        out: list[FetchCopyHint] = []
        for summary in self.state.copies_by_id.values():
            image = self.planner_store.image_fixture(str(summary.image))
            if (collection_key, path) in image.covered_files:
                out.append(FetchCopyHint(id=summary.id, location=summary.location))
        return out


class AcceptancePlanningService:
    def __init__(self, *, planner_store: AcceptancePlannerStore) -> None:
        self.planner_store = planner_store
        self._iso_service = ImageRootPlanningService(
            image_lookup=self.planner_store.get_image_root_record,
            plan_lookup=self.planner_store.get_plan_payload,
        )

    def get_plan(self) -> object:
        return self.planner_store.get_plan_payload()

    def get_image(self, image_id: str) -> object:
        return self.planner_store.get_image_payload(image_id)

    async def get_iso_stream(self, image_id: str) -> object:
        return await self._iso_service.get_iso_stream(image_id)


@dataclass(slots=True)
class ApiPlanAndImagesAcceptanceSystem:
    client: TestClient
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    collection_service: AcceptanceCollectionService
    planner_store: AcceptancePlannerStore
    copy_store: AcceptanceCopyStore

    def get_plan(self):
        return self.client.get("/v1/plan")

    def get_image(self, image_id: str):
        return self.client.get(f"/v1/images/{image_id}")

    def get_iso(self, image_id: str):
        return self.client.get(f"/v1/images/{image_id}/iso")

    def register_copy(self, image_id: str, *, copy_id: str, location: str):
        return self.client.post(f"/v1/images/{image_id}/copies", json={"id": copy_id, "location": location})

    def get_collection(self, collection_id: str):
        return self.client.get(f"/v1/collections/{collection_id}")



def _invoke_factory(factory: Any, system: ApiPlanAndImagesAcceptanceSystem) -> object:
    dependency_map = {
        "catalog": system.catalog,
        "catalog_repo": system.catalog,
        "repo": system.catalog,
        "state": system.state,
        "collection_service": system.collection_service,
        "collections": system.collection_service,
        "planner": system.planner_store,
        "planner_store": system.planner_store,
        "plan_store": system.planner_store,
        "image_store": system.planner_store,
        "images": system.planner_store,
        "copy_store": system.copy_store,
        "copy_registry": system.copy_store,
        "image_lookup": system.planner_store.get_image_root_record,
        "plan_lookup": system.planner_store.get_plan_payload,
    }
    signature = inspect.signature(factory)
    kwargs: dict[str, object] = {}
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            continue
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        if parameter.name in dependency_map:
            kwargs[parameter.name] = dependency_map[parameter.name]
            continue
        if parameter.default is inspect.Parameter.empty:
            raise AssertionError(
                "Acceptance test could not instantiate the production planning/copy service. "
                f"Unsupported constructor parameter: {parameter.name!r} on {factory!r}."
            )
    return factory(**kwargs)



def _build_planning_service(system: ApiPlanAndImagesAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.planning")

    builder = getattr(module, "build_planning_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and candidate not in {StubPlanningService, ImageRootPlanningService}
        and all(callable(getattr(candidate, name, None)) for name in ("get_plan", "get_image", "get_iso_stream"))
    ]
    if not classes:
        return StubPlanningService()

    classes.sort(key=lambda candidate: candidate.__name__)
    return _invoke_factory(classes[0], system)



def _build_copy_service(system: ApiPlanAndImagesAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.copies")

    builder = getattr(module, "build_copy_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and candidate is not StubCopyService
        and callable(getattr(candidate, "register", None))
    ]
    if not classes:
        return StubCopyService()

    classes.sort(key=lambda candidate: candidate.__name__)
    return _invoke_factory(classes[0], system)



def _write_file(root: Path, relative_path: str, content: bytes) -> None:
    file_path = root / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(content)



def _seed_archive_fixtures(root: Path) -> tuple[Path, Path]:
    docs_root = root / DOCS_COLLECTION_ID
    media_root = root / "media"

    docs_files = {
        "reports/q1.txt": b"quarter one report\n",
        "invoices/april.txt": b"april invoice\n",
        "receipts/may.txt": b"may receipt\n",
    }
    media_files = {
        "photos/set-a/cover.jpg": b"jpeg-data-placeholder\n",
        "photos/set-a/notes.txt": b"scanned photo notes\n",
    }

    for relative_path, content in docs_files.items():
        _write_file(docs_root, relative_path, content)
    for relative_path, content in media_files.items():
        _write_file(media_root, relative_path, content)

    return docs_root, media_root



def _seed_image_root(root: Path, image_id: str, files: dict[str, bytes]) -> Path:
    image_root = root / image_id
    for relative_path, content in files.items():
        _write_file(image_root, relative_path, content)
    return image_root



def _install_fake_xorriso(bin_dir: Path) -> Path:
    script_path = bin_dir / "xorriso"
    script_path.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path


def mapped_sources(argv: list[str]) -> list[Path]:
    out: list[Path] = []
    index = 0
    while index < len(argv):
        if argv[index] == '-map' and index + 2 < len(argv):
            out.append(Path(argv[index + 1]))
            index += 3
            continue
        index += 1
    return out


def source_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(candidate.stat().st_size for candidate in path.rglob('*') if candidate.is_file())
    return 0


argv = sys.argv[1:]
sources = mapped_sources(argv)
total_bytes = sum(source_bytes(path) for path in sources)
if '-print-size' in argv:
    blocks = max(1, math.ceil(total_bytes / 2048))
    sys.stderr.write(f'size={blocks}\n')
    raise SystemExit(0)

payload = b'FAKEISO\0' + b'\n'.join(str(path).encode('utf-8') for path in sorted(sources)) + b'\n'
sys.stdout.buffer.write(payload if payload else b'FAKEISO\0\n')
"""
    )
    script_path.chmod(0o755)
    return script_path


@pytest.fixture
def api_plan_and_images_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiPlanAndImagesAcceptanceSystem]:
    docs_root, media_root = _seed_archive_fixtures(tmp_path / "archive")
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    _install_fake_xorriso(fake_bin_dir)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ.get('PATH', '')}")

    image_roots_dir = tmp_path / "images"
    first_image_root = _seed_image_root(
        image_roots_dir,
        IMAGE_ID,
        {
            "README.txt": b"disc one readme\n",
            "manifest.json": b'{"image": "img_2026-04-20_01"}\n',
            "payload/docs/reports/q1.txt": b"quarter one report\n",
            "payload/docs/invoices/april.txt": b"april invoice\n",
        },
    )
    second_image_root = _seed_image_root(
        image_roots_dir,
        SECOND_IMAGE_ID,
        {
            "README.txt": b"disc two readme\n",
            "manifest.json": b'{"image": "img_2026-04-20_02"}\n',
            "payload/media/photos/set-a/cover.jpg": b"jpeg-data-placeholder\n",
        },
    )

    state = AcceptanceState()
    state.seed_collection_from_tree(DOCS_COLLECTION_ID, docs_root, fully_hot=True, fully_archived=False)
    state.seed_collection_from_tree("media", media_root, fully_hot=True, fully_archived=False)

    planner_store = AcceptancePlannerStore(state)
    planner_store.seed_image(
        PlannerImageFixture(
            id=ImageId(IMAGE_ID),
            volume_id="ARC-IMG-20260420-01",
            filename=f"{IMAGE_ID}.iso",
            image_root=first_image_root,
            bytes=8_200,
            files=2,
            collections=[DOCS_COLLECTION_ID],
            iso_ready=True,
            covered_files=[
                (CollectionId(DOCS_COLLECTION_ID), "reports/q1.txt"),
                (CollectionId(DOCS_COLLECTION_ID), "invoices/april.txt"),
            ],
        )
    )
    planner_store.seed_image(
        PlannerImageFixture(
            id=ImageId(SECOND_IMAGE_ID),
            volume_id="ARC-IMG-20260420-02",
            filename=f"{SECOND_IMAGE_ID}.iso",
            image_root=second_image_root,
            bytes=6_100,
            files=1,
            collections=["media"],
            iso_ready=False,
            covered_files=[(CollectionId("media"), "photos/set-a/cover.jpg")],
        )
    )

    catalog = AcceptanceCatalogRepo(state)
    collection_service = AcceptanceCollectionService(catalog=catalog)
    copy_store = AcceptanceCopyStore(state=state, planner_store=planner_store)

    app = create_app()
    with TestClient(app) as client:
        system = ApiPlanAndImagesAcceptanceSystem(
            client=client,
            state=state,
            catalog=catalog,
            collection_service=collection_service,
            planner_store=planner_store,
            copy_store=copy_store,
        )
        planning_service = _build_planning_service(system)
        copy_service = _build_copy_service(system)
        app.dependency_overrides[get_container] = lambda: ServiceContainer(
            collections=collection_service,
            search=StubSearchService(),
            planning=planning_service,
            copies=copy_service,
            pins=StubPinService(),
            fetches=StubFetchService(),
        )
        try:
            yield system
        finally:
            app.dependency_overrides.clear()



def _assert_error_code(payload: dict[str, Any], *, code: str) -> None:
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


class TestReadCurrentPlan:
    """Covers: tests/acceptance/features/api.plan_and_images.feature :: Scenario: Read the current plan."""

    def test_read_current_plan(self, api_plan_and_images_system: ApiPlanAndImagesAcceptanceSystem) -> None:
        assert api_plan_and_images_system.planner_store.has_candidate_image()

        response = api_plan_and_images_system.get_plan()

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"ready", "target_bytes", "min_fill_bytes", "images", "unplanned_bytes", "note"} or set(payload) == {"ready", "target_bytes", "min_fill_bytes", "images", "unplanned_bytes"}
        assert isinstance(payload["ready"], bool)
        assert payload["target_bytes"] > 0
        assert payload["min_fill_bytes"] > 0
        assert isinstance(payload["images"], list)
        assert payload["images"], "expected at least one candidate image"

        for image in payload["images"]:
            assert set(image) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}
            assert image["fill"] == pytest.approx(image["bytes"] / payload["target_bytes"])

        fills = [image["fill"] for image in payload["images"]]
        assert fills == sorted(fills, reverse=True)
        assert payload["images"][0]["id"] == IMAGE_ID


class TestReadOneImageSummary:
    """Covers: tests/acceptance/features/api.plan_and_images.feature :: Scenario: Read one image summary."""

    def test_read_one_image_summary(self, api_plan_and_images_system: ApiPlanAndImagesAcceptanceSystem) -> None:
        assert api_plan_and_images_system.planner_store.image_exists(IMAGE_ID)

        response = api_plan_and_images_system.get_image(IMAGE_ID)

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == IMAGE_ID
        assert set(payload) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}
        assert payload["collections"] == [DOCS_COLLECTION_ID]


class TestDownloadIsoForReadyImage:
    """Covers: tests/acceptance/features/api.plan_and_images.feature :: Scenario: Download an ISO for a ready image."""

    def test_download_iso_for_a_ready_image(self, api_plan_and_images_system: ApiPlanAndImagesAcceptanceSystem) -> None:
        assert api_plan_and_images_system.planner_store.image_fixture(IMAGE_ID).iso_ready is True

        response = api_plan_and_images_system.get_iso(IMAGE_ID)

        assert response.status_code == 200
        assert response.content
        assert response.content.startswith(b"FAKEISO\0") or response.headers["content-type"] == "application/octet-stream"


class TestRegisteringACopyIncreasesArchivedCoverage:
    """Covers: tests/acceptance/features/api.plan_and_images.feature :: Rule: Registering a copy increases archived coverage."""

    def test_register_a_physical_copy(self, api_plan_and_images_system: ApiPlanAndImagesAcceptanceSystem) -> None:
        before = api_plan_and_images_system.get_collection(DOCS_COLLECTION_ID)
        assert before.status_code == 200
        before_payload = before.json()

        response = api_plan_and_images_system.register_copy(IMAGE_ID, copy_id="BR-021-A", location="Shelf B1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["copy"]["id"] == "BR-021-A"
        assert payload["copy"]["image"] == IMAGE_ID
        assert payload["copy"]["location"] == "Shelf B1"
        assert payload["copy"]["created_at"]

        after = api_plan_and_images_system.get_collection(DOCS_COLLECTION_ID)
        assert after.status_code == 200
        after_payload = after.json()
        assert after_payload["archived_bytes"] > before_payload["archived_bytes"]
        assert after_payload["pending_bytes"] < before_payload["pending_bytes"]

    def test_reusing_a_copy_id_fails(self, api_plan_and_images_system: ApiPlanAndImagesAcceptanceSystem) -> None:
        api_plan_and_images_system.copy_store.seed_existing_copy(
            image_id=IMAGE_ID,
            copy_id="BR-021-A",
            location="Shelf B1",
        )

        response = api_plan_and_images_system.register_copy(IMAGE_ID, copy_id="BR-021-A", location="Shelf B2")

        assert response.status_code == 409
        _assert_error_code(response.json(), code="conflict")
