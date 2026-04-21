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
from arc_core.domain.errors import HashMismatch, InvalidState, NotFound
from arc_core.domain.models import CollectionSummary, FetchCopyHint, FetchSummary, FileRef, PinSummary
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CollectionId, CopyId, EntryId, FetchId, Sha256Hex, TargetStr
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService


FEATURE_PATH = "tests/acceptance/features/api.fetches.feature"
INVOICE_TARGET = "docs:/tax/2022/invoice-123.pdf"


@dataclass(frozen=True, slots=True)
class CopySeed:
    id: CopyId
    location: str
    disc_path: str
    enc: dict[str, object]

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, location=self.location)


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool = False
    archived: bool = True
    copies: list[CopySeed] = field(default_factory=list)

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
    copies_by_id: dict[CopyId, CopySeed] = field(default_factory=dict)

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

    def attach_copy(self, raw_target: str, copy_seed: CopySeed) -> None:
        self.copies_by_id[copy_seed.id] = copy_seed
        for record in self.selected_files(raw_target):
            record.copies.append(copy_seed)

    def set_hot(self, raw_target: str, *, hot: bool) -> None:
        for record in self.selected_files(raw_target):
            record.hot = hot

    def set_archived(self, raw_target: str, *, archived: bool) -> None:
        for record in self.selected_files(raw_target):
            record.archived = archived

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
        raise AssertionError(
            f"create_collection_from_scan should not be called by {FEATURE_PATH}: {collection_id=} {staging_path=}"
        )

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

    def resolve_target_files(self, target: object) -> list[FileRef]:
        canonical = getattr(target, "canonical", str(target))
        selected = self.state.selected_files(str(canonical))
        return [
            FileRef(
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                copies=[copy_seed.hint for copy_seed in record.copies],
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

    def ensure_target_visible(self, target: object) -> None:
        canonical = getattr(target, "canonical", str(target))
        for record in self.state.selected_files(str(canonical)):
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
class FetchEntryRecord:
    id: EntryId
    path: str
    bytes: int
    sha256: Sha256Hex
    content: bytes
    copies: list[CopySeed]
    uploaded_content: bytes | None = None


@dataclass(slots=True)
class FetchRecord:
    summary: FetchSummary
    entries: dict[EntryId, FetchEntryRecord]


@dataclass(slots=True)
class AcceptanceFetchStore:
    ids: AcceptanceIds
    state: AcceptanceState
    hot_store: AcceptanceHotStore
    fetches: dict[FetchId, FetchRecord] = field(default_factory=dict)

    def find_reusable_fetch(self, target: TargetStr) -> FetchSummary | None:
        for record in self.fetches.values():
            if record.summary.target != target:
                continue
            if record.summary.state in {FetchState.DONE, FetchState.FAILED}:
                continue
            return record.summary
        return None

    def create_fetch(self, target: TargetStr, entries: list[object], copies: list[object]) -> FetchSummary:
        fetch_id = FetchId(self.ids.fetch_id())
        normalized_entries = self._normalize_entries(entries=entries, copies=copies)
        summary = self._make_summary(fetch_id=fetch_id, target=target, entries=normalized_entries, state=FetchState.WAITING_MEDIA)
        self.fetches[fetch_id] = FetchRecord(summary=summary, entries={entry.id: entry for entry in normalized_entries})
        return summary

    def seed_fetch(
        self,
        *,
        fetch_id: str,
        target: str,
        entry_id: str = "e1",
        content: bytes | None = None,
        state: FetchState = FetchState.WAITING_MEDIA,
    ) -> None:
        selected = self.state.selected_files(target)
        if not selected:
            raise AssertionError(f"seed_fetch needs at least one selected file for {target!r}")
        source = selected[0]
        entry_content = content if content is not None else source.content
        entry_sha = cast(Sha256Hex, hashlib.sha256(entry_content).hexdigest())
        copies = list(source.copies)
        entry = FetchEntryRecord(
            id=EntryId(entry_id),
            path=source.path,
            bytes=len(entry_content),
            sha256=entry_sha,
            content=entry_content,
            copies=copies,
        )
        fetch_key = FetchId(fetch_id)
        summary = self._make_summary(fetch_id=fetch_key, target=cast(TargetStr, parse_target(target).canonical), entries=[entry], state=state)
        self.fetches[fetch_key] = FetchRecord(summary=summary, entries={entry.id: entry})

    def get_fetch(self, fetch_id: FetchId) -> FetchSummary:
        return self._get_record(fetch_id).summary

    def get_manifest(self, fetch_id: FetchId) -> object:
        record = self._get_record(fetch_id)
        return {
            "id": str(record.summary.id),
            "target": str(record.summary.target),
            "entries": [self._manifest_entry(entry) for entry in record.entries.values()],
        }

    def accept_uploaded_entry(self, fetch_id: FetchId, entry_id: EntryId, sha256: str, content: bytes) -> object:
        record = self._get_record(fetch_id)
        entry = record.entries.get(entry_id)
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        actual_sha = hashlib.sha256(content).hexdigest()
        if sha256 != entry.sha256 or actual_sha != entry.sha256:
            raise HashMismatch("sha256 did not match expected entry hash")
        entry.uploaded_content = content
        if record.summary.state == FetchState.WAITING_MEDIA:
            self._set_state(fetch_id, FetchState.UPLOADING)
        return {
            "entry": str(entry.id),
            "accepted": True,
            "bytes": len(content),
        }

    def can_complete(self, fetch_id: FetchId) -> bool:
        record = self._get_record(fetch_id)
        return all(entry.uploaded_content is not None for entry in record.entries.values())

    def mark_verifying(self, fetch_id: FetchId) -> None:
        self._set_state(fetch_id, FetchState.VERIFYING)

    def mark_done(self, fetch_id: FetchId) -> None:
        record = self._get_record(fetch_id)
        for entry in record.entries.values():
            if entry.uploaded_content is None:
                continue
            self.hot_store.put_file(entry.sha256, entry.uploaded_content)
        self._set_state(fetch_id, FetchState.DONE)

    def mark_failed(self, fetch_id: FetchId, reason: str) -> None:
        if not reason:
            raise AssertionError("mark_failed should include a reason")
        self._set_state(fetch_id, FetchState.FAILED)

    def upload_all_required_entries(self, fetch_id: str) -> None:
        record = self._get_record(FetchId(fetch_id))
        for entry in record.entries.values():
            entry.uploaded_content = entry.content
            self.hot_store.put_file(entry.sha256, entry.content)
        if record.summary.state == FetchState.WAITING_MEDIA:
            self._set_state(FetchId(fetch_id), FetchState.UPLOADING)

    def _get_record(self, fetch_id: FetchId) -> FetchRecord:
        try:
            return self.fetches[fetch_id]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

    def _set_state(self, fetch_id: FetchId, state: FetchState) -> None:
        record = self._get_record(fetch_id)
        record.summary = FetchSummary(
            id=record.summary.id,
            target=record.summary.target,
            state=state,
            files=record.summary.files,
            bytes=record.summary.bytes,
            copies=list(record.summary.copies),
        )

    def _make_summary(
        self,
        *,
        fetch_id: FetchId,
        target: TargetStr,
        entries: list[FetchEntryRecord],
        state: FetchState,
    ) -> FetchSummary:
        summary_copies: list[FetchCopyHint] = []
        seen_copy_ids: set[CopyId] = set()
        for entry in entries:
            for copy_seed in entry.copies:
                if copy_seed.id in seen_copy_ids:
                    continue
                seen_copy_ids.add(copy_seed.id)
                summary_copies.append(copy_seed.hint)
        return FetchSummary(
            id=fetch_id,
            target=target,
            state=state,
            files=len(entries),
            bytes=sum(entry.bytes for entry in entries),
            copies=summary_copies,
        )

    def _normalize_entries(self, *, entries: list[object], copies: list[object]) -> list[FetchEntryRecord]:
        fallback_copies = self._normalize_copies(copies)
        normalized: list[FetchEntryRecord] = []
        for raw_entry in entries:
            entry_id = EntryId(str(self._get_attr(raw_entry, "id", default=self.ids.entry_id())))
            path = str(self._get_attr(raw_entry, "path"))
            content = self._resolve_content(raw_entry, path)
            sha256 = cast(
                Sha256Hex,
                str(self._get_attr(raw_entry, "sha256", default=hashlib.sha256(content).hexdigest())),
            )
            entry_copies = self._normalize_copies(self._get_attr(raw_entry, "copies", default=fallback_copies))
            normalized.append(
                FetchEntryRecord(
                    id=entry_id,
                    path=path,
                    bytes=len(content),
                    sha256=sha256,
                    content=content,
                    copies=entry_copies,
                )
            )
        return normalized

    def _resolve_content(self, raw_entry: object, path: str) -> bytes:
        explicit_content = self._get_attr(raw_entry, "content", default=None)
        if isinstance(explicit_content, bytes):
            return explicit_content

        collection_id = self._get_attr(raw_entry, "collection_id", default=None)
        if collection_id is not None:
            record = self.state.files_by_collection.get(CollectionId(str(collection_id)), {}).get(path)
            if record is not None:
                return record.content

        sha256 = self._get_attr(raw_entry, "sha256", default=None)
        if sha256 is not None:
            candidates = self.state.files_by_sha256.get(cast(Sha256Hex, str(sha256)), [])
            if candidates:
                return candidates[0].content

        raise AssertionError(f"could not resolve entry content for path {path!r} in {FEATURE_PATH}")

    def _normalize_copies(self, copies: object) -> list[CopySeed]:
        normalized: list[CopySeed] = []
        for raw_copy in copies if isinstance(copies, list) else list(copies):
            if isinstance(raw_copy, CopySeed):
                copy_seed = raw_copy
            else:
                copy_id = CopyId(str(self._get_attr(raw_copy, "id", alias="copy", default="")))
                copy_seed = self.state.copies_by_id.get(copy_id)
                if copy_seed is None:
                    location = str(self._get_attr(raw_copy, "location", default="optical media"))
                    copy_seed = CopySeed(
                        id=copy_id,
                        location=location,
                        disc_path=str(self._get_attr(raw_copy, "disc_path", default=f"/{copy_id}.age")),
                        enc=cast(dict[str, object], self._get_attr(raw_copy, "enc", default={"alg": "age"})),
                    )
                    self.state.copies_by_id[copy_seed.id] = copy_seed
            normalized.append(copy_seed)
        return normalized

    def _manifest_entry(self, entry: FetchEntryRecord) -> dict[str, object]:
        return {
            "id": str(entry.id),
            "path": entry.path,
            "bytes": entry.bytes,
            "sha256": str(entry.sha256),
            "copies": [
                {
                    "copy": str(copy_seed.id),
                    "location": copy_seed.location,
                    "disc_path": copy_seed.disc_path,
                    "enc": copy_seed.enc,
                }
                for copy_seed in entry.copies
            ],
        }

    @staticmethod
    def _get_attr(raw: object, name: str, *, alias: str | None = None, default: object = inspect._empty) -> object:
        if isinstance(raw, dict):
            if name in raw:
                return raw[name]
            if alias is not None and alias in raw:
                return raw[alias]
        else:
            if hasattr(raw, name):
                return getattr(raw, name)
            if alias is not None and hasattr(raw, alias):
                return getattr(raw, alias)
        if default is not inspect._empty:
            return default
        raise AssertionError(f"missing required attribute {name!r} on {raw!r}")


@dataclass(slots=True)
class ApiFetchesAcceptanceSystem:
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

    def post_pin(self, raw_target: str):
        return self.client.post("/v1/pin", json={"target": raw_target})

    def get_fetch(self, fetch_id: str):
        return self.client.get(f"/v1/fetches/{fetch_id}")

    def get_manifest(self, fetch_id: str):
        return self.client.get(f"/v1/fetches/{fetch_id}/manifest")

    def upload_entry(self, fetch_id: str, entry_id: str, sha256: str, content: bytes):
        return self.client.put(
            f"/v1/fetches/{fetch_id}/files/{entry_id}",
            headers={"X-Sha256": sha256, "Content-Type": "application/octet-stream"},
            content=content,
        )

    def complete_fetch(self, fetch_id: str):
        return self.client.post(f"/v1/fetches/{fetch_id}/complete")

    def get_pins(self):
        return self.client.get("/v1/pins")


def _docs_fixture_tree(root: Path) -> Path:
    docs_root = root / "docs"
    files = {
        "tax/2022/invoice-123.pdf": b"invoice 123 contents\n",
        "tax/2022/receipt-456.pdf": b"receipt 456 contents\n",
    }
    for relative_path, content in files.items():
        file_path = docs_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return docs_root


def _invoke_factory(factory: Any, system: ApiFetchesAcceptanceSystem, extra: dict[str, object] | None = None) -> object:
    dependency_map: dict[str, object] = {
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
    if extra:
        dependency_map.update(extra)

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


def _build_fetch_service(system: ApiFetchesAcceptanceSystem) -> object:
    module = importlib.import_module("arc_core.services.fetches")

    builder = getattr(module, "build_fetch_service", None)
    if callable(builder):
        return _invoke_factory(builder, system)

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and all(callable(getattr(candidate, name, None)) for name in ("get", "manifest", "upload_entry", "complete"))
    ]
    if not classes:
        return StubFetchService()

    classes.sort(key=lambda candidate: (candidate.__name__.startswith("Stub"), candidate.__name__))
    return _invoke_factory(classes[0], system)


def _build_pin_service(system: ApiFetchesAcceptanceSystem, *, fetch_service: object) -> object:
    module = importlib.import_module("arc_core.services.pins")

    builder = getattr(module, "build_pin_service", None)
    if callable(builder):
        return _invoke_factory(builder, system, extra={"fetch_service": fetch_service, "fetches_service": fetch_service})

    classes = [
        candidate
        for _, candidate in inspect.getmembers(module, inspect.isclass)
        if candidate.__module__ == module.__name__
        and all(callable(getattr(candidate, name, None)) for name in ("pin", "release", "list_pins"))
    ]
    if not classes:
        return StubPinService()

    classes.sort(key=lambda candidate: (candidate.__name__.startswith("Stub"), candidate.__name__))
    return _invoke_factory(classes[0], system, extra={"fetch_service": fetch_service, "fetches_service": fetch_service})


@pytest.fixture
def api_fetches_system(tmp_path: Path) -> Iterator[ApiFetchesAcceptanceSystem]:
    state = AcceptanceState()
    state.seed_collection_from_tree("docs", _docs_fixture_tree(tmp_path), fully_hot=False)
    state.set_archived(INVOICE_TARGET, archived=True)
    state.set_hot(INVOICE_TARGET, hot=False)
    state.attach_copy(
        INVOICE_TARGET,
        CopySeed(
            id=CopyId("cp-1"),
            location="vault-shelf-a",
            disc_path="/disc/docs-tax-2022/invoice-123.pdf.age",
            enc={"alg": "age", "recipient": "test-recipient"},
        ),
    )

    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)
    projection_store = AcceptanceProjectionStore(state)
    ids = AcceptanceIds()
    fetch_store = AcceptanceFetchStore(ids=ids, state=state, hot_store=hot_store)

    app = create_app()
    with TestClient(app) as client:
        system = ApiFetchesAcceptanceSystem(
            client=client,
            state=state,
            catalog=catalog,
            hot_store=hot_store,
            projection_store=projection_store,
            fetch_store=fetch_store,
            ids=ids,
        )
        fetch_service = _build_fetch_service(system)
        pin_service = _build_pin_service(system, fetch_service=fetch_service)
        app.dependency_overrides[get_container] = lambda: ServiceContainer(
            collections=StubCollectionService(),
            search=StubSearchService(),
            planning=StubPlanningService(),
            copies=StubCopyService(),
            pins=pin_service,
            fetches=fetch_service,
        )
        try:
            yield system
        finally:
            app.dependency_overrides.clear()


def _assert_error_code(payload: dict[str, Any], *, code: str) -> None:
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def _assert_pin_list(response_json: dict[str, Any], *, expected_targets: set[str]) -> None:
    assert set(item["target"] for item in response_json["pins"]) == expected_targets
    assert len(response_json["pins"]) == len(expected_targets)


class TestPinningColdArchivedDataCreatesFetches:
    """Covers: tests/acceptance/features/api.fetches.feature :: Rule: Pinning cold archived data creates a fetch."""

    def test_pin_a_cold_archived_file(self, api_fetches_system: ApiFetchesAcceptanceSystem) -> None:
        response = api_fetches_system.post_pin(INVOICE_TARGET)

        assert response.status_code == 200
        payload = response.json()
        assert payload["target"] == INVOICE_TARGET
        assert payload["pin"] is True
        assert payload["hot"]["state"] == "waiting"
        assert payload["hot"]["present_bytes"] == 0
        assert payload["hot"]["missing_bytes"] == api_fetches_system.state.selected_bytes(INVOICE_TARGET)
        assert payload["fetch"] is not None
        assert payload["fetch"]["id"]
        assert payload["fetch"]["state"] == "waiting_media"

    def test_repeating_the_same_pin_reuses_the_active_fetch(self, api_fetches_system: ApiFetchesAcceptanceSystem) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-existing", target=INVOICE_TARGET)

        response = api_fetches_system.post_pin(INVOICE_TARGET)

        assert response.status_code == 200
        payload = response.json()
        assert payload["target"] == INVOICE_TARGET
        assert payload["pin"] is True
        assert payload["fetch"] is not None
        assert payload["fetch"]["id"] == "fx-existing"
        assert payload["fetch"]["state"] == "waiting_media"


class TestFetchManifestsAreStableAndComplete:
    """Covers: tests/acceptance/features/api.fetches.feature :: Rule: Fetch manifests are stable and complete."""

    def test_read_a_fetch_summary(self, api_fetches_system: ApiFetchesAcceptanceSystem) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-1", target=INVOICE_TARGET)

        response = api_fetches_system.get_fetch("fx-1")

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"id", "target", "state", "files", "bytes", "copies"}
        assert payload["id"] == "fx-1"
        assert payload["target"] == INVOICE_TARGET
        assert payload["state"] == "waiting_media"
        assert payload["files"] == 1
        assert payload["bytes"] == api_fetches_system.state.selected_bytes(INVOICE_TARGET)
        assert payload["copies"] == [{"id": "cp-1", "location": "vault-shelf-a"}]

    def test_read_the_manifest_twice(self, api_fetches_system: ApiFetchesAcceptanceSystem) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-1", target=INVOICE_TARGET)

        first = api_fetches_system.get_manifest("fx-1")
        second = api_fetches_system.get_manifest("fx-1")

        assert first.status_code == 200
        assert second.status_code == 200
        first_payload = first.json()
        second_payload = second.json()
        assert [entry["id"] for entry in first_payload["entries"]] == [entry["id"] for entry in second_payload["entries"]]
        assert [
            {"path": entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
            for entry in first_payload["entries"]
        ] == [
            {"path": entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
            for entry in second_payload["entries"]
        ]


class TestFetchUploadAndCompletionAreHashVerified:
    """Covers: tests/acceptance/features/api.fetches.feature :: Rule: Fetch upload and completion are hash-verified."""

    def test_uploading_bytes_with_the_wrong_hash_fails(self, api_fetches_system: ApiFetchesAcceptanceSystem) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-1", target=INVOICE_TARGET, entry_id="e1")

        response = api_fetches_system.upload_entry(
            fetch_id="fx-1",
            entry_id="e1",
            sha256="wrong-hash",
            content=b"incorrect plaintext bytes\n",
        )

        assert response.status_code == 409
        _assert_error_code(response.json(), code="hash_mismatch")

    def test_completing_before_all_required_entries_are_present_fails(
        self,
        api_fetches_system: ApiFetchesAcceptanceSystem,
    ) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-1", target=INVOICE_TARGET, entry_id="e1")

        response = api_fetches_system.complete_fetch("fx-1")

        assert response.status_code == 409
        _assert_error_code(response.json(), code="invalid_state")

    def test_completing_a_fully_uploaded_fetch_materializes_the_target(
        self,
        api_fetches_system: ApiFetchesAcceptanceSystem,
    ) -> None:
        api_fetches_system.seed_exact_pin(INVOICE_TARGET)
        api_fetches_system.fetch_store.seed_fetch(fetch_id="fx-1", target=INVOICE_TARGET, entry_id="e1")

        manifest_response = api_fetches_system.get_manifest("fx-1")
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        for entry in manifest["entries"]:
            stored_file = api_fetches_system.state.selected_files(INVOICE_TARGET)[0]
            upload = api_fetches_system.upload_entry(
                fetch_id="fx-1",
                entry_id=entry["id"],
                sha256=entry["sha256"],
                content=stored_file.content,
            )
            assert upload.status_code == 200
            assert upload.json() == {"entry": entry["id"], "accepted": True, "bytes": len(stored_file.content)}

        response = api_fetches_system.complete_fetch("fx-1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "fx-1"
        assert payload["state"] == "done"
        assert payload["hot"]["state"] == "ready"
        assert payload["hot"]["present_bytes"] == api_fetches_system.state.selected_bytes(INVOICE_TARGET)
        assert payload["hot"]["missing_bytes"] == 0
        assert api_fetches_system.state.is_hot(INVOICE_TARGET)

        pins = api_fetches_system.get_pins()
        assert pins.status_code == 200
        _assert_pin_list(pins.json(), expected_targets={INVOICE_TARGET})
