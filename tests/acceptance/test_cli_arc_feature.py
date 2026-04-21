from __future__ import annotations

import hashlib
import importlib
import inspect
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
from arc_core.domain.models import CollectionSummary, FetchCopyHint, FetchSummary, FileRef, PinSummary, Target
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CollectionId, CopyId, FetchId, Sha256Hex, TargetStr
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/cli.arc.feature"
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


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
        files: Mapping[str, bytes],
        *,
        hot_paths: set[str],
        archived_paths: set[str],
        copy_map: Mapping[str, list[AcceptanceCopyHint]],
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
class LiveArcSystem:
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    hot_store: AcceptanceHotStore
    projection_store: AcceptanceProjectionStore
    fetch_store: AcceptanceFetchStore
    ids: AcceptanceIds
    base_url: str
    shutdown: Any

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.request(method, path, params=params, json=json_body)
        return response

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        pythonpath_parts = [str(SRC_ROOT)]
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        return subprocess.run(
            [sys.executable, "-m", "arc_cli.main", *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


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
            except Exception as exc:  # pragma: no cover - exercised only while booting server
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(f"Timed out waiting for live arc test server at {self.base_url}") from last_error

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():  # pragma: no cover - defensive cleanup guard
            raise RuntimeError("Timed out stopping live arc test server")


@dataclass(slots=True)
class _PortReservation:
    bound_socket: socket.socket
    port: int

    def close(self) -> None:
        self.bound_socket.close()

    def __enter__(self) -> _PortReservation:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()



def _reserve_local_port() -> _PortReservation:
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen(1)
    port = cast(int, reserved.getsockname()[1])
    return _PortReservation(bound_socket=reserved, port=port)



def _build_search_service(system: LiveArcSystem) -> object:
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



def _build_pin_service(system: LiveArcSystem) -> object:
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



def _build_planning_service(system: LiveArcSystem) -> object:
    module = importlib.import_module("arc_core.services.planning")

    builder = getattr(module, "build_planning_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and all(callable(getattr(candidate, name, None)) for name in ("get_plan", "get_image", "get_iso_stream"))
        and candidate.__name__ not in {"StubPlanningService", "ImageRootPlanningService"}
    ]
    if not classes:
        return StubPlanningService()

    classes.sort(key=lambda candidate: candidate.__name__)
    return _invoke_factory(classes[0], system)



def _invoke_factory(factory: Any, system: LiveArcSystem) -> object:
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
def live_arc_system() -> Iterator[LiveArcSystem]:
    state = AcceptanceState()
    state.seed_collection(
        "docs",
        {
            "tax/2022/invoice-123.pdf": b"invoice 123 contents\n",
            "tax/2022/receipt-456.pdf": b"receipt 456 contents\n",
            "letters/cover.txt": b"cover letter\n",
        },
        hot_paths={"tax/2022/receipt-456.pdf", "letters/cover.txt"},
        archived_paths={"tax/2022/invoice-123.pdf", "tax/2022/receipt-456.pdf"},
        copy_map={
            "tax/2022/invoice-123.pdf": [
                AcceptanceCopyHint(id=CopyId("copy-docs-1"), location="vault-a/shelf-01"),
                AcceptanceCopyHint(id=CopyId("copy-docs-2"), location="vault-a/shelf-02"),
            ],
            "tax/2022/receipt-456.pdf": [
                AcceptanceCopyHint(id=CopyId("copy-docs-3"), location="vault-a/shelf-03"),
            ],
        },
    )
    state.seed_collection(
        "photos-2024",
        {
            "albums/japan/day-01.jpg": b"japan day 01\n",
            "albums/iceland/day-01.jpg": b"iceland day 01\n",
        },
        hot_paths=set(),
        archived_paths={"albums/japan/day-01.jpg", "albums/iceland/day-01.jpg"},
        copy_map={
            "albums/japan/day-01.jpg": [
                AcceptanceCopyHint(id=CopyId("copy-photos-1"), location="vault-b/bin-07"),
            ],
        },
    )
    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)
    projection_store = AcceptanceProjectionStore(state)
    ids = AcceptanceIds()
    fetch_store = AcceptanceFetchStore(ids=ids)

    app = create_app()
    system = LiveArcSystem(
        state=state,
        catalog=catalog,
        hot_store=hot_store,
        projection_store=projection_store,
        fetch_store=fetch_store,
        ids=ids,
        base_url="",
        shutdown=None,
    )
    app.dependency_overrides[get_container] = lambda: ServiceContainer(
        collections=StubCollectionService(),
        search=_build_search_service(system),
        planning=_build_planning_service(system),
        copies=StubCopyService(),
        pins=_build_pin_service(system),
        fetches=StubFetchService(),
    )

    with _reserve_local_port() as reservation:
        host = "127.0.0.1"
        port = reservation.port
        reservation.close()

        server = _LiveServerHandle(app, host=host, port=port)
        server.start()
        system.base_url = server.base_url
        system.shutdown = server.close
        try:
            yield system
        finally:
            app.dependency_overrides.clear()
            server.close()



def _assert_cli_success(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, (
        f"{FEATURE_PATH} expected exit code 0, got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result.stdout.strip()



def _assert_json_stdout_matches_endpoint(
    system: LiveArcSystem,
    command: list[str],
    *,
    method: str,
    path: str,
    params: Mapping[str, object] | None = None,
    json_body: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    command_result = system.run_arc(*command)
    stdout = _assert_cli_success(command_result)
    cli_payload = json.loads(stdout)

    response = system.request(method, path, params=params, json_body=json_body)
    assert response.status_code == 200, response.text
    assert cli_payload == response.json()
    return cli_payload



def _assert_not_json(text: str) -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


class TestCliJsonModeMirrorsApiPayloads:
    """Covers: tests/acceptance/features/cli.arc.feature :: Rule: JSON mode mirrors API payloads."""

    def test_arc_pin_emits_the_api_pin_payload(self, live_arc_system: LiveArcSystem) -> None:
        _assert_json_stdout_matches_endpoint(
            live_arc_system,
            ["pin", "docs:/tax/2022/invoice-123.pdf", "--json"],
            method="POST",
            path="/v1/pin",
            json_body={"target": "docs:/tax/2022/invoice-123.pdf"},
        )

    def test_arc_release_emits_the_api_release_payload(self, live_arc_system: LiveArcSystem) -> None:
        _assert_json_stdout_matches_endpoint(
            live_arc_system,
            ["release", "docs:/tax/2022/invoice-123.pdf", "--json"],
            method="POST",
            path="/v1/release",
            json_body={"target": "docs:/tax/2022/invoice-123.pdf"},
        )

    def test_arc_find_emits_the_api_search_payload(self, live_arc_system: LiveArcSystem) -> None:
        _assert_json_stdout_matches_endpoint(
            live_arc_system,
            ["find", "invoice", "--json"],
            method="GET",
            path="/v1/search",
            params={"q": "invoice", "limit": 25},
        )

    def test_arc_plan_emits_the_api_plan_payload(self, live_arc_system: LiveArcSystem) -> None:
        _assert_json_stdout_matches_endpoint(
            live_arc_system,
            ["plan", "--json"],
            method="GET",
            path="/v1/plan",
        )


class TestCliHumanModeRemainsConciseAndStable:
    """Covers: tests/acceptance/features/cli.arc.feature :: Rule: Non-JSON mode remains concise and stable."""

    def test_arc_pin_prints_fetch_guidance_when_recovery_is_needed(self, live_arc_system: LiveArcSystem) -> None:
        command_result = live_arc_system.run_arc("pin", "docs:/tax/2022/invoice-123.pdf")
        stdout = _assert_cli_success(command_result)
        _assert_not_json(stdout)

        response = live_arc_system.request(
            "POST",
            "/v1/pin",
            json_body={"target": "docs:/tax/2022/invoice-123.pdf"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert stdout.count("docs:/tax/2022/invoice-123.pdf") >= 1
        assert payload["fetch"] is not None, f"{FEATURE_PATH} expected pinning docs:/tax/2022/invoice-123.pdf to require recovery"
        assert stdout.count(payload["fetch"]["id"]) >= 1
        candidate_copy_ids = [copy["id"] for copy in payload["fetch"]["copies"]]
        assert candidate_copy_ids, f"{FEATURE_PATH} expected at least one candidate copy id in the fetch hint"
        assert any(copy_id in stdout for copy_id in candidate_copy_ids)
