from __future__ import annotations

import base64
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

HELPER_ROOT = Path(__file__).resolve().parent
if str(HELPER_ROOT) not in sys.path:
    sys.path.insert(0, str(HELPER_ROOT))
from typing import Any, cast

import httpx
import pytest
import uvicorn

from arc_api.app import create_app
from arc_api.deps import ServiceContainer, get_container
from arc_core.domain.errors import HashMismatch
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CopyId, FetchId, TargetStr
from arc_core.services.collections import StubCollectionService
from arc_core.services.copies import StubCopyService
from arc_core.services.fetches import StubFetchService
from arc_core.services.pins import StubPinService
from arc_core.services.planning import StubPlanningService
from arc_core.services.search import StubSearchService
from test_api_fetches_feature import (
    AcceptanceCatalogRepo,
    AcceptanceFetchStore,
    AcceptanceHotStore,
    AcceptanceIds,
    AcceptanceProjectionStore,
    AcceptanceState,
    CopySeed,
)


FEATURE_PATH = "tests/acceptance/features/cli.arc_disc.feature"
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
FETCH_ID = "fx-1"
FETCH_TARGET = "docs:/tax/2022/"
DEVICE_PATH = "/dev/fake-sr0"
INVOICE_PATH = "tax/2022/invoice-123.pdf"
RECEIPT_PATH = "tax/2022/receipt-456.pdf"


@dataclass(slots=True)
class TrackingFetchStore(AcceptanceFetchStore):
    rejected_upload_codes: list[str] = field(default_factory=list)

    def accept_uploaded_entry(self, fetch_id: FetchId, entry_id: str, sha256: str, content: bytes) -> object:
        try:
            return super().accept_uploaded_entry(fetch_id=fetch_id, entry_id=entry_id, sha256=sha256, content=content)
        except HashMismatch:
            self.rejected_upload_codes.append("hash_mismatch")
            raise


@dataclass(slots=True)
class LiveArcDiscSystem:
    state: AcceptanceState
    catalog: AcceptanceCatalogRepo
    hot_store: AcceptanceHotStore
    projection_store: AcceptanceProjectionStore
    fetch_store: TrackingFetchStore
    ids: AcceptanceIds
    base_url: str
    shutdown: Any
    runner_path: Path
    fixture_path: Path

    def request(self, method: str, path: str, *, params: Mapping[str, object] | None = None) -> httpx.Response:
        with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
            return client.request(method, path, params=params)

    def get_fetch(self, fetch_id: str = FETCH_ID) -> httpx.Response:
        return self.request("GET", f"/v1/fetches/{fetch_id}")

    def run_arc_disc(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        pythonpath_parts = [str(SRC_ROOT)]
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        env["ARC_DISC_FIXTURE_PATH"] = str(self.fixture_path)
        return subprocess.run(
            [sys.executable, str(self.runner_path), *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_fetch(self) -> None:
        state_target = cast(TargetStr, parse_target(FETCH_TARGET).canonical)
        entries = self.catalog.resolve_target_files(parse_target(FETCH_TARGET))
        copies = [copy_hint for entry in entries for copy_hint in entry.copies]
        summary = self.fetch_store.create_fetch(state_target, entries=entries, copies=copies)
        assert str(summary.id) == FETCH_ID, f"{FEATURE_PATH} expected seeded fetch id {FETCH_ID}, got {summary.id}"

    def configure_disc_fixture(
        self,
        *,
        fail_path: str | None = None,
        bad_plaintext_path: str | None = None,
    ) -> None:
        manifest = cast(dict[str, Any], self.fetch_store.get_manifest(FetchId(FETCH_ID)))
        files_by_path = {record.path: record.content for record in self.state.selected_files(FETCH_TARGET)}

        encrypted_by_disc_path: dict[str, str] = {}
        plaintext_by_fixture_key: dict[str, str] = {}
        fail_disc_paths: list[str] = []

        for entry in manifest["entries"]:
            entry_path = str(entry["path"])
            copy_info = entry["copies"][0]
            disc_path = str(copy_info["disc_path"])
            fixture_key = str(copy_info["enc"]["fixture_key"])
            plaintext = files_by_path[entry_path]
            if entry_path == bad_plaintext_path:
                plaintext = plaintext + b"corrupted-by-fixture\n"
            encrypted = f"ciphertext::{fixture_key}".encode("utf-8")
            encrypted_by_disc_path[disc_path] = base64.b64encode(encrypted).decode("ascii")
            plaintext_by_fixture_key[fixture_key] = base64.b64encode(plaintext).decode("ascii")
            if entry_path == fail_path:
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

    def fetch_state(self, fetch_id: str = FETCH_ID) -> str:
        return str(self.fetch_store.get_fetch(FetchId(fetch_id)).state.value)

    def fetch_target_is_hot(self, fetch_id: str = FETCH_ID) -> bool:
        target = str(self.fetch_store.get_fetch(FetchId(fetch_id)).target)
        return self.state.is_hot(target)


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
    port = int(reserved.getsockname()[1])
    return _PortReservation(bound_socket=reserved, port=port)


def _docs_fixture_tree(root: Path) -> Path:
    docs_root = root / "docs"
    files = {
        INVOICE_PATH: b"invoice 123 contents\n",
        RECEIPT_PATH: b"receipt 456 contents\n",
    }
    for relative_path, content in files.items():
        file_path = docs_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return docs_root


def _write_arc_disc_runner(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import arc_disc.main as arc_disc_main

_FIXTURE = json.loads(Path(os.environ["ARC_DISC_FIXTURE_PATH"]).read_text(encoding="utf-8"))


class FixtureOpticalReader:
    def read(self, disc_path: str, *, device: str) -> bytes:
        fail_disc_paths = set(_FIXTURE.get("reader", {}).get("fail_disc_paths", []))
        if disc_path in fail_disc_paths:
            raise RuntimeError(f"fixture optical read failed for {disc_path} on {device}")
        try:
            encoded = _FIXTURE["reader"]["encrypted_by_disc_path"][disc_path]
        except KeyError as exc:
            raise RuntimeError(f"missing encrypted fixture for {disc_path}") from exc
        return base64.b64decode(encoded)


class FixtureCrypto:
    def decrypt_entry(self, encrypted: bytes, enc: dict[str, object]) -> bytes:
        fixture_key = str(enc["fixture_key"])
        try:
            encoded = _FIXTURE["crypto"]["plaintext_by_fixture_key"][fixture_key]
        except KeyError as exc:
            raise RuntimeError(f"missing plaintext fixture for {fixture_key}") from exc
        return base64.b64decode(encoded)


arc_disc_main.PlaceholderOpticalReader = FixtureOpticalReader
arc_disc_main.PlaceholderCrypto = FixtureCrypto

if __name__ == "__main__":
    arc_disc_main.main()
""".lstrip(),
        encoding="utf-8",
    )


def _invoke_factory(factory: Any, system: LiveArcDiscSystem) -> object:
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
                "Acceptance test could not instantiate the production fetch service. "
                f"Unsupported constructor parameter: {parameter.name!r} on {factory!r}."
            )
    return factory(**kwargs)


def _build_fetch_service(system: LiveArcDiscSystem) -> object:
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


@pytest.fixture
def live_arc_disc_system(tmp_path: Path) -> Iterator[LiveArcDiscSystem]:
    state = AcceptanceState()
    state.seed_collection_from_tree("docs", _docs_fixture_tree(tmp_path), fully_hot=False)
    state.attach_copy(
        "docs:/tax/2022/invoice-123.pdf",
        CopySeed(
            id=CopyId("cp-invoice"),
            location="vault-shelf-a",
            disc_path="/disc/docs-tax-2022/invoice-123.pdf.age",
            enc={"alg": "age", "fixture_key": INVOICE_PATH},
        ),
    )
    state.attach_copy(
        "docs:/tax/2022/receipt-456.pdf",
        CopySeed(
            id=CopyId("cp-receipt"),
            location="vault-shelf-b",
            disc_path="/disc/docs-tax-2022/receipt-456.pdf.age",
            enc={"alg": "age", "fixture_key": RECEIPT_PATH},
        ),
    )

    catalog = AcceptanceCatalogRepo(state)
    hot_store = AcceptanceHotStore(state)
    projection_store = AcceptanceProjectionStore(state)
    ids = AcceptanceIds()
    fetch_store = TrackingFetchStore(ids=ids, state=state, hot_store=hot_store)

    runner_path = tmp_path / "arc_disc_runner.py"
    fixture_path = tmp_path / "arc_disc_fixture.json"
    _write_arc_disc_runner(runner_path)

    app = create_app()
    system = LiveArcDiscSystem(
        state=state,
        catalog=catalog,
        hot_store=hot_store,
        projection_store=projection_store,
        fetch_store=fetch_store,
        ids=ids,
        base_url="",
        shutdown=None,
        runner_path=runner_path,
        fixture_path=fixture_path,
    )
    app.dependency_overrides[get_container] = lambda: ServiceContainer(
        collections=StubCollectionService(),
        search=StubSearchService(),
        planning=StubPlanningService(),
        copies=StubCopyService(),
        pins=StubPinService(),
        fetches=_build_fetch_service(system),
    )

    with _reserve_local_port() as reservation:
        host = "127.0.0.1"
        port = reservation.port
        reservation.close()

        server = _LiveServerHandle(app, host=host, port=port)
        server.start()
        system.base_url = server.base_url
        system.shutdown = server.close
        system.seed_fetch()
        try:
            yield system
        finally:
            app.dependency_overrides.clear()
            server.close()


def _assert_success(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, (
        f"{FEATURE_PATH} expected exit code 0, got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def _assert_failure(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, (
        f"{FEATURE_PATH} expected non-zero exit code for failure scenario.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


class TestArcDiscCli:
    """Covers: tests/acceptance/features/cli.arc_disc.feature"""

    def test_fetch_completes_a_recoverable_fetch(self, live_arc_disc_system: LiveArcDiscSystem) -> None:
        live_arc_disc_system.configure_disc_fixture()

        result = live_arc_disc_system.run_arc_disc("fetch", FETCH_ID, "--device", DEVICE_PATH, "--json")

        payload = _assert_success(result)
        assert payload["id"] == FETCH_ID
        assert payload["state"] == "done"
        assert payload["hot"]["state"] == "ready"
        assert payload["hot"]["missing_bytes"] == 0
        assert live_arc_disc_system.fetch_state(FETCH_ID) == "done"
        assert live_arc_disc_system.fetch_target_is_hot(FETCH_ID)

    def test_fetch_fails_if_optical_recovery_fails(self, live_arc_disc_system: LiveArcDiscSystem) -> None:
        live_arc_disc_system.configure_disc_fixture(fail_path=INVOICE_PATH)

        result = live_arc_disc_system.run_arc_disc("fetch", FETCH_ID, "--device", DEVICE_PATH)

        _assert_failure(result)
        assert "fixture optical read failed" in result.stderr
        assert live_arc_disc_system.fetch_state(FETCH_ID) != "done"
        assert not live_arc_disc_system.fetch_target_is_hot(FETCH_ID)

    def test_fetch_fails_if_decrypted_bytes_do_not_match_expected_hash(
        self,
        live_arc_disc_system: LiveArcDiscSystem,
    ) -> None:
        live_arc_disc_system.configure_disc_fixture(bad_plaintext_path=RECEIPT_PATH)

        result = live_arc_disc_system.run_arc_disc("fetch", FETCH_ID, "--device", DEVICE_PATH)

        _assert_failure(result)
        assert "hash_mismatch" in live_arc_disc_system.fetch_store.rejected_upload_codes
        assert live_arc_disc_system.fetch_state(FETCH_ID) != "done"
        assert not live_arc_disc_system.fetch_target_is_hot(FETCH_ID)
