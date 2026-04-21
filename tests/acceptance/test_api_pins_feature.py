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
from arc_core.domain.types import CollectionId, FetchId, Sha256Hex, TargetStr
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/api.pins.feature"


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool = False
    archived: bool = True
    copies: list[FetchCopyHint] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        digest = hashlib.sha256(self.content).hexdigest()
        return cast(Sha256Hex, digest)


@dataclass(slots=True)
class AcceptanceState:
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    files_by_sha256: dict[Sha256Hex, list[StoredFile]] = field(default_factory=dict)
    exact_pins: set[TargetStr] = field(default_factory=set)

    def seed_collection_from_tree(self, collection_id: str, root: Path, *, fully_hot: bool) -> None:
        cid = CollectionId(collection_id)
        collection_files: dict[str, StoredFile] = {}
        for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
            relative_path = file_path.relative_to(root).as_posix()
            record = StoredFile(
                collection_id=cid,
                path=relative_path,
                content=file_path.read_bytes(),
                hot=fully_hot,
            )
            collection_files[relative_path] = record
            self.files_by_sha256.setdefault(record.sha256, []).append(record)
        self.files_by_collection[cid] = collection_files

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


@dataclass(slots=True)
class AcceptanceCatalogRepo:
    state: AcceptanceState

    def collection_exists(self, collection_id: CollectionId) -> bool:
        return collection_id in self.state.files_by_collection

    def create_collection_from_scan(self, collection_id: CollectionId, staging_path: str) -> CollectionSummary:
        raise AssertionError(f"create_collection_from_scan should not be called by {FEATURE_PATH}: {collection_id=} {staging_path=}")

    def get_collection_summary(self, collection_id: CollectionId) -> CollectionSummary:
        files = list(self.state.files_by_collection.get(collection_id, {}).values())
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

    def resolve_target_files(self, target: Target) -> list[FileRef]:
        selected = self.state.selected_files(target.canonical)
        return [
            FileRef(
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                copies=list(record.copies),
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
        for record in self.state.files_by_collection.get(collection_id, {}).values():
            record.hot = True

    def has_file(self, collection_id: CollectionId, path: str) -> bool:
        record = self.state.files_by_collection.get(collection_id, {}).get(path)
        return record.hot if record is not None else False

    def put_file(self, sha256: Sha256Hex, content: bytes) -> None:
        for record in self.state.files_by_sha256.get(sha256, []):
            if record.content == content:
                record.hot = True

    def hot_bytes_for_collection(self, collection_id: CollectionId) -> int:
        return sum(record.bytes for record in self.state.files_by_collection.get(collection_id, {}).values() if record.hot)


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
    entry_counter: int = 0

    def fetch_id(self) -> str:
        self.fetch_counter += 1
        return f"fx-{self.fetch_counter}"

    def entry_id(self) -> str:
        self.entry_counter += 1
        return f"e-{self.entry_counter}"


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
class ApiPinsAcceptanceSystem:
    client: TestClient
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    hot_store: AcceptanceHotStore
    projection_store: AcceptanceProjectionStore
    fetch_store: AcceptanceFetchStore
    ids: AcceptanceIds

    def seed_exact_pin(self, raw_target: str) -> None:
        canonical = cast(TargetStr, parse_target(raw_target).canonical)
        self.catalog.add_pin(canonical)
        self.projection_store.ensure_target_visible(parse_target(raw_target))

    def ensure_not_pinned(self, raw_target: str) -> None:
        canonical = cast(TargetStr, parse_target(raw_target).canonical)
        self.catalog.remove_pin(canonical)

    def post_pin(self, raw_target: str):
        return self.client.post("/v1/pin", json={"target": raw_target})

    def post_release(self, raw_target: str):
        return self.client.post("/v1/release", json={"target": raw_target})

    def get_pins(self):
        return self.client.get("/v1/pins")


def _docs_fixture_tree(root: Path) -> Path:
    docs_root = root / "docs"
    files = {
        "tax/2022/invoice-123.pdf": b"invoice 123 contents\n",
        "tax/2022/receipt-456.pdf": b"receipt 456 contents\n",
        "taxes/summary.txt": b"taxes summary\n",
        "letters/cover.txt": b"cover letter\n",
    }
    for relative_path, content in files.items():
        file_path = docs_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return docs_root


def _build_pin_service(system: ApiPinsAcceptanceSystem) -> object:
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


def _invoke_factory(factory: Any, system: ApiPinsAcceptanceSystem) -> object:
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
                "Acceptance test could not instantiate the production pin service. "
                f"Unsupported constructor parameter: {parameter.name!r} on {factory!r}."
            )
    return factory(**kwargs)


@pytest.fixture
def api_pins_system(tmp_path: Path) -> Iterator[ApiPinsAcceptanceSystem]:
    state = AcceptanceState()
    state.seed_collection_from_tree("docs", _docs_fixture_tree(tmp_path), fully_hot=True)
    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)
    projection_store = AcceptanceProjectionStore(state)
    ids = AcceptanceIds()
    fetch_store = AcceptanceFetchStore(ids=ids)

    app = create_app()
    with TestClient(app) as client:
        system = ApiPinsAcceptanceSystem(
            client=client,
            state=state,
            catalog=catalog,
            hot_store=hot_store,
            projection_store=projection_store,
            fetch_store=fetch_store,
            ids=ids,
        )
        pin_service = _build_pin_service(system)
        app.dependency_overrides[get_container] = lambda: ServiceContainer(
            collections=StubCollectionService(),
            search=StubSearchService(),
            planning=StubPlanningService(),
            copies=StubCopyService(),
            pins=pin_service,
            fetches=StubFetchService(),
        )
        try:
            yield system
        finally:
            app.dependency_overrides.clear()


def _assert_ready_pin_payload(payload: dict[str, Any], *, target: str, present_bytes: int) -> None:
    assert payload == {
        "target": target,
        "pin": True,
        "hot": {
            "state": "ready",
            "present_bytes": present_bytes,
            "missing_bytes": 0,
        },
        "fetch": None,
    }


def _assert_release_payload(payload: dict[str, Any], *, target: str) -> None:
    assert payload == {
        "target": target,
        "pin": False,
    }


def _assert_error_code(payload: dict[str, Any], *, code: str) -> None:
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def _assert_pin_list(response_json: dict[str, Any], *, expected_targets: set[str]) -> None:
    assert set(item["target"] for item in response_json["pins"]) == expected_targets
    assert len(response_json["pins"]) == len(expected_targets)


class TestPinningIsExactTargetIdempotent:
    """Covers: tests/acceptance/features/api.pins.feature :: Rule: Pinning is exact-target idempotent."""

    def test_pin_whole_collection_that_is_already_hot(self, api_pins_system: ApiPinsAcceptanceSystem) -> None:
        response = api_pins_system.post_pin("docs")

        assert response.status_code == 200
        _assert_ready_pin_payload(
            response.json(),
            target="docs",
            present_bytes=api_pins_system.state.selected_bytes("docs"),
        )

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={"docs"})

    def test_pin_single_file_that_is_already_hot(self, api_pins_system: ApiPinsAcceptanceSystem) -> None:
        response = api_pins_system.post_pin("docs:/tax/2022/invoice-123.pdf")

        assert response.status_code == 200
        _assert_ready_pin_payload(
            response.json(),
            target="docs:/tax/2022/invoice-123.pdf",
            present_bytes=api_pins_system.state.selected_bytes("docs:/tax/2022/invoice-123.pdf"),
        )

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={"docs:/tax/2022/invoice-123.pdf"})

    def test_repeating_the_same_pin_does_not_create_duplicates(self, api_pins_system: ApiPinsAcceptanceSystem) -> None:
        api_pins_system.seed_exact_pin("docs")

        response = api_pins_system.post_pin("docs")

        assert response.status_code == 200
        _assert_ready_pin_payload(
            response.json(),
            target="docs",
            present_bytes=api_pins_system.state.selected_bytes("docs"),
        )

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={"docs"})
        assert sum(item["target"] == "docs" for item in pins.json()["pins"]) == 1


class TestReleasingRemovesOnlyTheExactMatchingPin:
    """Covers: tests/acceptance/features/api.pins.feature :: Rule: Releasing removes only the exact matching pin."""

    def test_releasing_a_broader_pin_leaves_the_narrower_pin_intact(
        self,
        api_pins_system: ApiPinsAcceptanceSystem,
    ) -> None:
        api_pins_system.seed_exact_pin("docs:/tax/")
        api_pins_system.seed_exact_pin("docs:/tax/2022/invoice-123.pdf")

        response = api_pins_system.post_release("docs:/tax/")

        assert response.status_code == 200
        _assert_release_payload(response.json(), target="docs:/tax/")

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={"docs:/tax/2022/invoice-123.pdf"})
        assert api_pins_system.state.is_hot("docs:/tax/2022/invoice-123.pdf")

    def test_releasing_a_narrower_pin_leaves_the_broader_pin_intact(
        self,
        api_pins_system: ApiPinsAcceptanceSystem,
    ) -> None:
        api_pins_system.seed_exact_pin("docs:/tax/")
        api_pins_system.seed_exact_pin("docs:/tax/2022/invoice-123.pdf")

        response = api_pins_system.post_release("docs:/tax/2022/invoice-123.pdf")

        assert response.status_code == 200
        _assert_release_payload(response.json(), target="docs:/tax/2022/invoice-123.pdf")

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={"docs:/tax/"})
        assert api_pins_system.state.is_hot("docs:/tax/2022/invoice-123.pdf")

    def test_releasing_a_missing_pin_is_a_successful_no_op(self, api_pins_system: ApiPinsAcceptanceSystem) -> None:
        api_pins_system.ensure_not_pinned("docs:/missing/")

        response = api_pins_system.post_release("docs:/missing/")

        assert response.status_code == 200
        _assert_release_payload(response.json(), target="docs:/missing/")

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets=set())


class TestSelectorsAreCanonicalAndPrecise:
    """Covers: tests/acceptance/features/api.pins.feature :: Rule: Selectors are canonical and precise."""

    @pytest.mark.parametrize(
        "target",
        [
            "docs:",
            "docs:raw/",
            "docs:/a/../b",
            "docs://raw/",
        ],
    )
    def test_invalid_targets_are_rejected_for_pin(
        self,
        api_pins_system: ApiPinsAcceptanceSystem,
        target: str,
    ) -> None:
        response = api_pins_system.post_pin(target)

        assert response.status_code == 400
        _assert_error_code(response.json(), code="invalid_target")

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets=set())

    @pytest.mark.parametrize(
        "target",
        [
            "docs:",
            "docs:raw/",
            "docs:/a/../b",
            "docs://raw/",
        ],
    )
    def test_invalid_targets_are_rejected_for_release(
        self,
        api_pins_system: ApiPinsAcceptanceSystem,
        target: str,
    ) -> None:
        response = api_pins_system.post_release(target)

        assert response.status_code == 400
        _assert_error_code(response.json(), code="invalid_target")

        pins = api_pins_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets=set())
