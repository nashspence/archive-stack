from __future__ import annotations

import hashlib
import importlib
import inspect
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from arc_api.app import create_app
from arc_api.deps import ServiceContainer, get_container
from arc_core.domain.enums import FetchState
from arc_core.domain.models import CollectionSummary, FetchCopyHint, FetchSummary, FileRef, PinSummary, Target
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CollectionId, CopyId, FetchId, Sha256Hex, TargetStr
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/api.search.feature"


@dataclass(frozen=True, slots=True)
class AcceptanceCopyHint:
    id: CopyId
    location: str


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool
    archived: bool
    copies: list[AcceptanceCopyHint] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        digest = hashlib.sha256(self.content).hexdigest()
        return cast(Sha256Hex, digest)

    @property
    def canonical_target(self) -> str:
        return f"{self.collection_id}:/{self.path}"


@dataclass(slots=True)
class AcceptanceState:
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    files_by_sha256: dict[Sha256Hex, list[StoredFile]] = field(default_factory=dict)
    exact_pins: set[TargetStr] = field(default_factory=set)

    def seed_collection(
        self,
        collection_id: str,
        files: dict[str, bytes],
        *,
        hot_paths: set[str],
        archived_paths: set[str],
        copy_map: dict[str, list[AcceptanceCopyHint]],
    ) -> None:
        cid = CollectionId(collection_id)
        collection_files: dict[str, StoredFile] = {}
        for relative_path, content in sorted(files.items()):
            normalized_path = relative_path.lstrip("/")
            record = StoredFile(
                collection_id=cid,
                path=normalized_path,
                content=content,
                hot=normalized_path in hot_paths,
                archived=normalized_path in archived_paths,
                copies=list(copy_map.get(normalized_path, [])),
            )
            collection_files[normalized_path] = record
            self.files_by_sha256.setdefault(record.sha256, []).append(record)
        self.files_by_collection[cid] = collection_files

    def collection_files(self, collection_id: CollectionId) -> list[StoredFile]:
        return list(self.files_by_collection.get(collection_id, {}).values())

    def selected_files(self, raw_target: str) -> list[StoredFile]:
        target = parse_target(raw_target)
        collection_files = self.files_by_collection.get(target.collection_id, {})
        if target.is_collection:
            return list(collection_files.values())

        assert target.path is not None
        logical_path = str(target.path).lstrip("/")
        if target.is_dir:
            prefix = logical_path.rstrip("/") + "/"
            return [record for record in collection_files.values() if record.path.startswith(prefix)]
        return [record for record in collection_files.values() if record.path == logical_path]

    def selected_bytes(self, raw_target: str) -> int:
        return sum(record.bytes for record in self.selected_files(raw_target))

    def is_hot(self, raw_target: str) -> bool:
        selected = self.selected_files(raw_target)
        return bool(selected) and all(record.hot for record in selected)

    def file_record(self, raw_target: str) -> StoredFile:
        selected = self.selected_files(raw_target)
        assert len(selected) == 1, f"{FEATURE_PATH} expected a single file for {raw_target!r}, got {len(selected)}"
        return selected[0]


@dataclass(slots=True)
class AcceptanceCatalogRepo:
    state: AcceptanceState

    def collection_exists(self, collection_id: CollectionId) -> bool:
        return collection_id in self.state.files_by_collection

    def create_collection_from_scan(self, collection_id: CollectionId, staging_path: str) -> CollectionSummary:
        raise AssertionError(f"create_collection_from_scan should not be called by {FEATURE_PATH}: {collection_id=} {staging_path=}")

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

    def search(self, query: str, limit: int) -> list[dict[str, object]]:
        needle = query.casefold()
        results: list[dict[str, object]] = []

        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            if needle in collection_name.casefold():
                summary = self.get_collection_summary(collection_id)
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

    def resolve_target_files(self, target: Target) -> list[FileRef]:
        selected = self.state.selected_files(target.canonical)
        return [
            FileRef(
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                copies=[FetchCopyHint(id=copy.id, location=copy.location) for copy in record.copies],
            )
            for record in selected
        ]

    def list_pins(self) -> list[PinSummary]:
        return [PinSummary(target=target) for target in sorted(self.state.exact_pins)]

    def has_exact_pin(self, target: TargetStr) -> bool:
        return target in self.state.exact_pins

    def add_pin(self, target: TargetStr) -> None:
        self.state.exact_pins.add(target)

    def remove_pin(self, target: TargetStr) -> None:
        self.state.exact_pins.discard(target)


@dataclass(slots=True)
class AcceptanceHotStore:
    state: AcceptanceState

    def materialize_closed_collection(self, collection_id: CollectionId) -> None:
        for record in self.state.collection_files(collection_id):
            record.hot = True

    def has_file(self, collection_id: CollectionId, path: str) -> bool:
        record = self.state.files_by_collection.get(collection_id, {}).get(path)
        return record.hot if record is not None else False

    def put_file(self, sha256: Sha256Hex, content: bytes) -> None:
        for record in self.state.files_by_sha256.get(sha256, []):
            if record.content == content:
                record.hot = True

    def hot_bytes_for_collection(self, collection_id: CollectionId) -> int:
        return sum(record.bytes for record in self.state.collection_files(collection_id) if record.hot)


@dataclass(slots=True)
class AcceptanceProjectionStore:
    state: AcceptanceState

    def reconcile_from_pins(self) -> None:
        selected_paths: set[tuple[CollectionId, str]] = set()
        for raw_target in self.state.exact_pins:
            for record in self.state.selected_files(raw_target):
                selected_paths.add((record.collection_id, record.path))
        for collection_files in self.state.files_by_collection.values():
            for record in collection_files.values():
                record.hot = (record.collection_id, record.path) in selected_paths

    def ensure_target_visible(self, target: Target) -> None:
        for record in self.state.selected_files(target.canonical):
            record.hot = True


@dataclass(slots=True)
class AcceptanceIds:
    fetch_counter: int = 0

    def fetch_id(self) -> str:
        self.fetch_counter += 1
        return f"fx-{self.fetch_counter}"


@dataclass(slots=True)
class AcceptanceFetchStore:
    ids: AcceptanceIds
    fetches: dict[FetchId, FetchSummary] = field(default_factory=dict)

    def find_reusable_fetch(self, target: TargetStr) -> FetchSummary | None:
        for fetch in self.fetches.values():
            if fetch.target != target:
                continue
            if fetch.state in {FetchState.DONE, FetchState.FAILED}:
                continue
            return fetch
        return None

    def create_fetch(self, target: TargetStr, entries: list[object], copies: list[object]) -> FetchSummary:
        fetch_id = FetchId(self.ids.fetch_id())
        summary = FetchSummary(
            id=fetch_id,
            target=target,
            state=FetchState.WAITING_MEDIA,
            files=len(entries),
            bytes=sum(int(getattr(entry, "bytes", 0)) for entry in entries),
            copies=[copy_hint for copy_hint in copies if isinstance(copy_hint, FetchCopyHint)],
        )
        self.fetches[fetch_id] = summary
        return summary

    def get_fetch(self, fetch_id: FetchId) -> FetchSummary:
        return self.fetches[fetch_id]

    def get_manifest(self, fetch_id: FetchId) -> object:
        raise AssertionError(f"get_manifest should not be called by {FEATURE_PATH}: {fetch_id=}")

    def accept_uploaded_entry(self, fetch_id: FetchId, entry_id: str, sha256: str, content: bytes) -> object:
        raise AssertionError(
            f"accept_uploaded_entry should not be called by {FEATURE_PATH}: {fetch_id=} {entry_id=} {sha256=} {len(content)=}"
        )

    def can_complete(self, fetch_id: FetchId) -> bool:
        raise AssertionError(f"can_complete should not be called by {FEATURE_PATH}: {fetch_id=}")

    def mark_verifying(self, fetch_id: FetchId) -> None:
        raise AssertionError(f"mark_verifying should not be called by {FEATURE_PATH}: {fetch_id=}")

    def mark_done(self, fetch_id: FetchId) -> None:
        raise AssertionError(f"mark_done should not be called by {FEATURE_PATH}: {fetch_id=}")

    def mark_failed(self, fetch_id: FetchId, reason: str) -> None:
        raise AssertionError(f"mark_failed should not be called by {FEATURE_PATH}: {fetch_id=} {reason=}")


@dataclass(slots=True)
class ApiSearchAcceptanceSystem:
    client: TestClient
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    hot_store: AcceptanceHotStore
    projection_store: AcceptanceProjectionStore
    fetch_store: AcceptanceFetchStore
    ids: AcceptanceIds

    def get_search(self, query: str, *, limit: int = 25):
        return self.client.get("/v1/search", params={"q": query, "limit": limit})

    def post_pin(self, raw_target: str):
        return self.client.post("/v1/pin", json={"target": raw_target})

    def post_release(self, raw_target: str):
        return self.client.post("/v1/release", json={"target": raw_target})

    def get_pins(self):
        return self.client.get("/v1/pins")


def _build_search_service(system: ApiSearchAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.search")

    builder = getattr(module, "build_search_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__ and callable(getattr(candidate, "search", None))
    ]
    if not classes:
        return StubSearchService()

    classes.sort(key=lambda candidate: (candidate.__name__.startswith("Stub"), candidate.__name__))
    return _invoke_factory(classes[0], system)


def _build_pin_service(system: ApiSearchAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.pins")

    builder = getattr(module, "build_pin_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and all(callable(getattr(candidate, name, None)) for name in ("pin", "release", "list_pins"))
    ]
    if not classes:
        return StubPinService()

    classes.sort(key=lambda candidate: (candidate.__name__.startswith("Stub"), candidate.__name__))
    return _invoke_factory(classes[0], system)


def _invoke_factory(factory: Any, system: ApiSearchAcceptanceSystem) -> object:
    dependency_map = {
        "catalog": system.catalog,
        "catalog_repo": system.catalog,
        "repo": system.catalog,
        "hot": system.hot_store,
        "hot_store": system.hot_store,
        "projection": system.projection_store,
        "projection_store": system.projection_store,
        "fetch_store": system.fetch_store,
        "fetches": system.fetch_store,
        "ids": system.ids,
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
                "Acceptance test could not instantiate the production service. "
                f"Unsupported constructor parameter: {parameter.name!r} on {factory!r}."
            )
    return factory(**kwargs)


@pytest.fixture
def api_search_system() -> Iterator[ApiSearchAcceptanceSystem]:
    state = AcceptanceState()
    state.seed_collection(
        "docs",
        {
            "tax/2022/invoice-123.pdf": b"invoice 123 contents\n",
            "tax/2022/receipt-456.pdf": b"receipt 456 contents\n",
            "letters/cover.txt": b"cover letter\n",
        },
        hot_paths={"tax/2022/invoice-123.pdf", "letters/cover.txt"},
        archived_paths={"tax/2022/invoice-123.pdf", "tax/2022/receipt-456.pdf"},
        copy_map={
            "tax/2022/invoice-123.pdf": [AcceptanceCopyHint(id=CopyId("copy-docs-1"), location="vault-a/shelf-01")],
            "tax/2022/receipt-456.pdf": [AcceptanceCopyHint(id=CopyId("copy-docs-2"), location="vault-a/shelf-02")],
        },
    )
    state.seed_collection(
        "photos-2024",
        {
            "albums/japan/day-01.jpg": b"japan day 01\n",
            "albums/japan/day-02.jpg": b"japan day 02\n",
            "albums/iceland/day-01.jpg": b"iceland day 01\n",
        },
        hot_paths=set(),
        archived_paths={"albums/japan/day-01.jpg", "albums/japan/day-02.jpg", "albums/iceland/day-01.jpg"},
        copy_map={
            "albums/japan/day-01.jpg": [AcceptanceCopyHint(id=CopyId("copy-photos-1"), location="vault-b/bin-07")],
            "albums/japan/day-02.jpg": [AcceptanceCopyHint(id=CopyId("copy-photos-2"), location="vault-b/bin-07")],
        },
    )
    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)
    projection_store = AcceptanceProjectionStore(state)
    ids = AcceptanceIds()
    fetch_store = AcceptanceFetchStore(ids=ids)

    app = create_app()
    with TestClient(app) as client:
        system = ApiSearchAcceptanceSystem(
            client=client,
            state=state,
            catalog=catalog,
            hot_store=hot_store,
            projection_store=projection_store,
            fetch_store=fetch_store,
            ids=ids,
        )
        app.dependency_overrides[get_container] = lambda: ServiceContainer(
            collections=StubCollectionService(),
            search=_build_search_service(system),
            planning=StubPlanningService(),
            copies=StubCopyService(),
            pins=_build_pin_service(system),
            fetches=StubFetchService(),
        )
        try:
            yield system
        finally:
            app.dependency_overrides.clear()


def _assert_search_file_shape(result: dict[str, Any]) -> None:
    assert result["kind"] == "file"
    assert parse_target(result["target"]).canonical == result["target"]
    assert isinstance(result["collection"], str) and result["collection"]
    assert isinstance(result["path"], str) and result["path"].startswith("/")
    assert isinstance(result["bytes"], int) and result["bytes"] >= 0
    assert isinstance(result["hot"], bool)
    assert isinstance(result["copies"], list)


def _assert_ready_or_waiting_pin_response(payload: dict[str, Any], *, target: str) -> None:
    assert payload["target"] == target
    assert payload["pin"] is True
    assert payload["hot"]["state"] in {"ready", "waiting"}
    assert isinstance(payload["hot"]["present_bytes"], int)
    assert isinstance(payload["hot"]["missing_bytes"], int)


def _assert_release_response(payload: dict[str, Any], *, target: str) -> None:
    assert payload == {"target": target, "pin": False}


class TestSearchReturnsFileAndCollectionTargets:
    """Covers: tests/acceptance/features/api.search.feature :: Scenario: Search returns file and collection targets."""

    def test_search_returns_canonical_file_results_with_hot_and_copy_state(
        self,
        api_search_system: ApiSearchAcceptanceSystem,
    ) -> None:
        response = api_search_system.get_search("invoice", limit=25)

        assert response.status_code == 200
        payload = response.json()
        assert payload["query"] == "invoice"

        file_results = [result for result in payload["results"] if result["kind"] == "file"]
        assert file_results, f"{FEATURE_PATH} expected at least one file result for q=invoice"

        for result in file_results:
            _assert_search_file_shape(result)

        invoice_result = next(result for result in file_results if result["target"] == "docs:/tax/2022/invoice-123.pdf")
        invoice_record = api_search_system.state.file_record(invoice_result["target"])
        assert invoice_result["hot"] is invoice_record.hot
        assert invoice_result["copies"] == [
            {"id": str(copy.id), "location": copy.location} for copy in invoice_record.copies
        ]


class TestSearchTargetsAreDirectlyReusable:
    """Covers: tests/acceptance/features/api.search.feature :: Scenario: Search targets are directly reusable."""

    def test_every_returned_target_can_be_fed_directly_into_pin_and_release(
        self,
        api_search_system: ApiSearchAcceptanceSystem,
    ) -> None:
        search_response = api_search_system.get_search("japan", limit=25)

        assert search_response.status_code == 200
        results = search_response.json()["results"]
        assert results, f"{FEATURE_PATH} expected at least one result for q=japan"

        for result in results:
            target = result["target"]
            assert parse_target(target).canonical == target

            pin_response = api_search_system.post_pin(target)
            assert pin_response.status_code == 200
            _assert_ready_or_waiting_pin_response(pin_response.json(), target=target)

            release_response = api_search_system.post_release(target)
            assert release_response.status_code == 200
            _assert_release_response(release_response.json(), target=target)

        pins_response = api_search_system.get_pins()
        assert pins_response.status_code == 200
        assert pins_response.json() == {"pins": []}


class TestSearchHonorsLimit:
    """Covers: tests/acceptance/features/api.search.feature :: Scenario: Search honors limit."""

    def test_search_limit_caps_the_number_of_returned_results(self, api_search_system: ApiSearchAcceptanceSystem) -> None:
        response = api_search_system.get_search("a", limit=1)

        assert response.status_code == 200
        assert len(response.json()["results"]) <= 1


class TestSearchIsCaseInsensitiveSubstringMatch:
    """Covers: tests/acceptance/features/api.search.feature :: Scenario: Search is case-insensitive substring match."""

    def test_uppercase_query_matches_the_same_canonical_file_target(
        self,
        api_search_system: ApiSearchAcceptanceSystem,
    ) -> None:
        response = api_search_system.get_search("INVOICE", limit=25)

        assert response.status_code == 200
        targets = {item["target"] for item in response.json()["results"]}
        assert "docs:/tax/2022/invoice-123.pdf" in targets
