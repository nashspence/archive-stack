from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arc_api.app import create_app
from arc_api.deps import ServiceContainer, get_container
from arc_core.domain.errors import NotFound
from arc_core.domain.models import CollectionSummary
from arc_core.domain.types import CollectionId
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/api.collections.feature"
STAGING_PATH = "/staging/photos-2024"
COLLECTION_ID = "photos-2024"


@dataclass(frozen=True, slots=True)
class StagedDirectory:
    virtual_path: str
    real_path: Path

    @property
    def collection_id(self) -> CollectionId:
        return CollectionId(PurePosixPath(self.virtual_path).name)

    def iter_files(self) -> list[Path]:
        return sorted(path for path in self.real_path.rglob("*") if path.is_file())

    @property
    def file_count(self) -> int:
        return len(self.iter_files())

    @property
    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.iter_files())


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool = False
    archived: bool = False

    @property
    def bytes(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class AcceptanceState:
    staged_directories: dict[str, StagedDirectory] = field(default_factory=dict)
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)

    def register_staged_directory(self, virtual_path: str, real_path: Path) -> None:
        self.staged_directories[virtual_path] = StagedDirectory(virtual_path=virtual_path, real_path=real_path)

    def staged_directory(self, virtual_path: str) -> StagedDirectory:
        return self.staged_directories[virtual_path]

    def seed_collection_from_tree(
        self,
        collection_id: str,
        root: Path,
        *,
        fully_hot: bool,
        fully_archived: bool = False,
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
        return list(self.files_by_collection.get(collection_id, {}).values())


@dataclass(slots=True)
class AcceptanceCatalogRepo:
    state: AcceptanceState

    def collection_exists(self, collection_id: CollectionId) -> bool:
        return collection_id in self.state.files_by_collection

    def create_collection_from_scan(self, collection_id: CollectionId, staging_path: str) -> CollectionSummary:
        staged = self.state.staged_directories.get(staging_path)
        if staged is None:
            raise NotFound(f"staged directory not found: {staging_path}")
        if staged.collection_id != collection_id:
            raise AssertionError(
                f"{FEATURE_PATH} expected collection id {staged.collection_id!r} from {staging_path!r}, "
                f"got {collection_id!r}"
            )
        self.state.seed_collection_from_tree(str(collection_id), staged.real_path, fully_hot=False)
        return self.get_collection_summary(collection_id)

    def get_collection_summary(self, collection_id: CollectionId) -> CollectionSummary:
        files = self.state.collection_files(collection_id)
        if not files:
            raise NotFound(f"collection not found: {collection_id}")
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

    def resolve_target_files(self, target: object) -> list[object]:
        raise AssertionError(f"resolve_target_files should not be called by {FEATURE_PATH}: {target=}")

    def list_pins(self) -> list[object]:
        raise AssertionError(f"list_pins should not be called by {FEATURE_PATH}")

    def has_exact_pin(self, target: object) -> bool:
        raise AssertionError(f"has_exact_pin should not be called by {FEATURE_PATH}: {target=}")

    def add_pin(self, target: object) -> None:
        raise AssertionError(f"add_pin should not be called by {FEATURE_PATH}: {target=}")

    def remove_pin(self, target: object) -> None:
        raise AssertionError(f"remove_pin should not be called by {FEATURE_PATH}: {target=}")


@dataclass(slots=True)
class AcceptanceHotStore:
    state: AcceptanceState

    def materialize_closed_collection(self, collection_id: CollectionId) -> None:
        for record in self.state.collection_files(collection_id):
            record.hot = True

    def has_file(self, collection_id: CollectionId, path: str) -> bool:
        record = self.state.files_by_collection.get(collection_id, {}).get(path)
        return record.hot if record is not None else False

    def put_file(self, sha256: str, content: bytes) -> None:
        raise AssertionError(f"put_file should not be called by {FEATURE_PATH}: {sha256=} {len(content)=}")

    def hot_bytes_for_collection(self, collection_id: CollectionId) -> int:
        return sum(record.bytes for record in self.state.collection_files(collection_id) if record.hot)


@dataclass(slots=True)
class ApiCollectionsAcceptanceSystem:
    client: TestClient
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    hot_store: AcceptanceHotStore

    def post_close(self, staging_path: str):
        return self.client.post("/v1/collections/close", json={"path": staging_path})

    def get_collection(self, collection_id: str):
        return self.client.get(f"/v1/collections/{collection_id}")

    @property
    def staged_fixture(self) -> StagedDirectory:
        return self.state.staged_directory(STAGING_PATH)

    def seed_archive_with_closed_collection(self) -> None:
        self.state.seed_collection_from_tree(
            COLLECTION_ID,
            self.staged_fixture.real_path,
            fully_hot=True,
            fully_archived=False,
        )


def _photos_fixture_tree(root: Path) -> Path:
    photos_root = root / COLLECTION_ID
    files = {
        "albums/japan/day-01.txt": b"arrived in tokyo\n",
        "albums/japan/day-02.txt": b"visited asakusa\n",
        "raw/img_0001.cr3": b"raw-image-0001\n",
        "raw/img_0002.cr3": b"raw-image-0002-longer\n",
    }
    for relative_path, content in files.items():
        file_path = photos_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return photos_root


def _invoke_factory(factory: Any, system: ApiCollectionsAcceptanceSystem) -> object:
    dependency_map = {
        "catalog": system.catalog,
        "catalog_repo": system.catalog,
        "repo": system.catalog,
        "hot": system.hot_store,
        "hot_store": system.hot_store,
        "state": system.state,
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
                "Acceptance test could not instantiate the production collection service. "
                f"Unsupported constructor parameter: {parameter.name!r} on {factory!r}."
            )
    return factory(**kwargs)


def _build_collection_service(system: ApiCollectionsAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.collections")

    builder = getattr(module, "build_collection_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__ and all(callable(getattr(candidate, name, None)) for name in ("close", "get"))
    ]
    if not classes:
        return StubCollectionService()

    classes.sort(key=lambda candidate: (candidate.__name__.startswith("Stub"), candidate.__name__))
    return _invoke_factory(classes[0], system)


@pytest.fixture
def api_collections_system(tmp_path: Path) -> Iterator[ApiCollectionsAcceptanceSystem]:
    state = AcceptanceState()
    state.register_staged_directory(STAGING_PATH, _photos_fixture_tree(tmp_path / "staging"))
    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)

    app = create_app()
    with TestClient(app) as client:
        system = ApiCollectionsAcceptanceSystem(
            client=client,
            state=state,
            catalog=catalog,
            hot_store=hot_store,
        )
        collection_service = _build_collection_service(system)
        app.dependency_overrides[get_container] = lambda: ServiceContainer(
            collections=collection_service,
            search=StubSearchService(),
            planning=StubPlanningService(),
            copies=StubCopyService(),
            pins=StubPinService(),
            fetches=StubFetchService(),
        )
        try:
            yield system
        finally:
            app.dependency_overrides.clear()


@pytest.fixture
def api_collections_seeded_system(api_collections_system: ApiCollectionsAcceptanceSystem) -> ApiCollectionsAcceptanceSystem:
    api_collections_system.seed_archive_with_closed_collection()
    return api_collections_system


def _assert_error_code(payload: dict[str, Any], *, code: str) -> None:
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def _assert_collection_summary(
    payload: dict[str, Any],
    *,
    collection_id: str,
    files: int,
    bytes_: int,
    hot_bytes: int,
    archived_bytes: int,
    pending_bytes: int,
) -> None:
    assert payload == {
        "id": collection_id,
        "files": files,
        "bytes": bytes_,
        "hot_bytes": hot_bytes,
        "archived_bytes": archived_bytes,
        "pending_bytes": pending_bytes,
    }


class TestClosingAStagedDirectoryCreatesOneHotCollection:
    """Covers: tests/acceptance/features/api.collections.feature :: Rule: Closing a staged directory creates one hot collection."""

    def test_close_a_staged_collection(self, api_collections_system: ApiCollectionsAcceptanceSystem) -> None:
        response = api_collections_system.post_close(STAGING_PATH)

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"collection"}
        _assert_collection_summary(
            payload["collection"],
            collection_id=COLLECTION_ID,
            files=api_collections_system.staged_fixture.file_count,
            bytes_=api_collections_system.staged_fixture.total_bytes,
            hot_bytes=api_collections_system.staged_fixture.total_bytes,
            archived_bytes=0,
            pending_bytes=api_collections_system.staged_fixture.total_bytes,
        )

        summary = api_collections_system.get_collection(COLLECTION_ID)
        assert summary.status_code == 200
        _assert_collection_summary(
            summary.json(),
            collection_id=COLLECTION_ID,
            files=api_collections_system.staged_fixture.file_count,
            bytes_=api_collections_system.staged_fixture.total_bytes,
            hot_bytes=api_collections_system.staged_fixture.total_bytes,
            archived_bytes=0,
            pending_bytes=api_collections_system.staged_fixture.total_bytes,
        )

    def test_reclosing_the_same_staged_path_fails(self, api_collections_system: ApiCollectionsAcceptanceSystem) -> None:
        first = api_collections_system.post_close(STAGING_PATH)
        assert first.status_code == 200

        second = api_collections_system.post_close(STAGING_PATH)

        assert second.status_code == 409
        _assert_error_code(second.json(), code="conflict")


class TestCollectionSummariesExposeStableCoverageFields:
    """Covers: tests/acceptance/features/api.collections.feature :: Rule: Collection summaries expose stable coverage fields."""

    def test_read_a_collection_summary(self, api_collections_seeded_system: ApiCollectionsAcceptanceSystem) -> None:
        response = api_collections_seeded_system.get_collection(COLLECTION_ID)

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"id", "files", "bytes", "hot_bytes", "archived_bytes", "pending_bytes"}
        assert payload["id"] == COLLECTION_ID
        assert payload["files"] == api_collections_seeded_system.staged_fixture.file_count
        assert payload["bytes"] == api_collections_seeded_system.staged_fixture.total_bytes
        assert payload["pending_bytes"] == payload["bytes"] - payload["archived_bytes"]
        assert 0 <= payload["hot_bytes"] <= payload["bytes"]
        assert 0 <= payload["archived_bytes"] <= payload["bytes"]

    def test_unknown_collection_returns_not_found(self, api_collections_system: ApiCollectionsAcceptanceSystem) -> None:
        response = api_collections_system.get_collection("missing")

        assert response.status_code == 404
        _assert_error_code(response.json(), code="not_found")
