from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from riverhog_adapter_api_client import RiverhogAdapterClient, RiverhogTusClient
from riverhog_adapters import app as adapter_app
from riverhog_adapters.app import AdapterComposition, create_app
from riverhog_adapters.config import AdapterConfig, SourceConfig
from riverhog_provenance import prepare_file_provenance

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient


class _Riverhog:
    def close(self) -> None:
        pass

    def list_archive_stores(self, *, per_page: int) -> dict[str, object]:
        assert per_page == 1
        return {"items": []}


class _Adapter:
    def status(self) -> dict[str, object]:
        return {
            "format": "riverhog-adapters-status/v1",
            "sources": [{"id": "camera-a", "adapter": "ftp", "claims": 0, "claim_bytes": 0}],
        }

    def run_once(self) -> dict[str, object]:
        return {
            "format": "riverhog-adapter-pass/v1",
            "completed": 0,
            "failed": [],
            "sources": ["camera-a"],
        }

    def flush(self, source_id: str) -> dict[str, object]:
        assert source_id == "camera-a"
        return {
            "format": "riverhog-adapter-pass/v1",
            "completed": 0,
            "failed": [],
            "sources": [source_id],
        }


class _Tus:
    def authenticate(self, _authorization: str | None) -> str:
        return "camera-a"

    def put_journal(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "accepted"}

    def put_binding(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "accepted"}

    def receipt(self, upload_id: str, **_kwargs: object) -> dict[str, object]:
        return {"upload_id": upload_id, "status": "pending"}

    def handle_hook(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


def _composition(tmp_path: Path) -> AdapterComposition:
    config = AdapterConfig(
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-token",
        api_token="adapter-token",
        sources=(
            SourceConfig(
                id="camera-a",
                adapter="ftp",
                root=tmp_path / "landing",
                ingest_source="ftp:camera-a",
                tags=("camera-a",),
                provenance="omit",
                provenance_omission_reason="Fixture has no host provenance.",
            ),
        ),
    )
    return AdapterComposition(config, _Riverhog(), _Adapter(), _Tus())  # type: ignore[arg-type]


def test_public_openapi_has_one_stable_operation_for_each_official_client_method(
    tmp_path: Path,
) -> None:
    application = create_app(_composition(tmp_path))
    schema = application.openapi()
    operations = {
        operation["operationId"]
        for path, item in schema["paths"].items()
        if path.startswith("/v1/")
        for method, operation in item.items()
        if method in {"get", "post", "put"}
    }

    assert operations == {
        "flush_adapter_source",
        "get_adapter_status",
        "get_tus_publication",
        "put_tus_provenance_binding",
        "put_tus_provenance_journal",
        "run_adapter_pass",
    }
    assert {"ErrorResponse", "HealthResponse"} <= set(schema["components"]["schemas"])
    assert all(
        "422" not in operation["responses"]
        for item in schema["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "responses" in operation
    )
    assert {
        "flush_adapter_source",
        "get_adapter_status",
        "run_adapter_pass",
    } <= set(dir(RiverhogAdapterClient))
    assert {
        "get_tus_publication",
        "put_tus_provenance_binding",
        "put_tus_provenance_journal",
    } <= set(dir(RiverhogTusClient))


def test_management_api_and_client_share_versioned_routes(tmp_path: Path) -> None:
    with TestClient(create_app(_composition(tmp_path))) as api:
        headers = {"Authorization": "Bearer adapter-token"}
        assert api.get("/v1/status", headers=headers).status_code == 200
        assert api.post("/v1/run", headers=headers).status_code == 200
        assert api.post("/v1/sources/camera-a/flush", headers=headers).status_code == 200
        response = api.get("/v1/status")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized"

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.raw_path.decode("ascii")))
        assert request.headers["authorization"] == "Bearer adapter-token"
        return httpx.Response(200, json={"status": "ok"})

    with RiverhogAdapterClient(
        "https://adapter.invalid",
        "adapter-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get_adapter_status()
        client.run_adapter_pass()
        client.flush_adapter_source("camera/a")

    assert requests == [
        ("GET", "/v1/status"),
        ("POST", "/v1/run"),
        ("POST", "/v1/sources/camera%2Fa/flush"),
    ]


def test_tus_client_uses_versioned_supplementary_routes() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.raw_path.decode("ascii")))
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"status": "pending"})

    with RiverhogTusClient(
        source="camera-a",
        password="secret",
        base_url="https://adapter.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.put_tus_provenance_journal("upload/a", "journal/a", b"\x1e{}\n")
        client.put_tus_provenance_binding("upload/a", {"status": "omitted"})
        client.get_tus_publication("upload/a")

    assert requests == [
        ("PUT", "/v1/tus-publications/upload%2Fa/journals/journal%2Fa"),
        ("PUT", "/v1/tus-publications/upload%2Fa/binding"),
        ("GET", "/v1/tus-publications/upload%2Fa"),
    ]


def test_tus_client_uses_the_configured_ingress_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RIVERHOG_ADAPTERS_INGRESS_URL", "https://ingress.invalid/base")

    with RiverhogTusClient(
        source="camera-a",
        password="secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    ) as client:
        assert client.base_url == "https://ingress.invalid/base"


def test_adapters_official_clients_positive_disposable_lifecycle(tmp_path: Path) -> None:
    application = create_app(_composition(tmp_path))
    observer = OperationObserver.install(application, application="riverhog-adapters")

    with TestClient(
        application,
        headers={"Authorization": "Bearer adapter-token"},
    ) as transport:
        management = RiverhogAdapterClient(
            "http://testserver",
            "adapter-token",
            allow_insecure_http=True,
        )
        management._http.close()  # type: ignore[attr-defined]
        management._http = TimeoutNeutralTestClient(  # type: ignore[assignment]
            transport,
            observer=observer,
        )
        assert management.adapter_health_live()["status"] == "ok"
        assert management.adapter_health_ready()["status"] == "ok"
        assert management.get_adapter_status()["format"] == "riverhog-adapters-status/v1"
        assert management.run_adapter_pass()["format"] == "riverhog-adapter-pass/v1"
        assert management.flush_adapter_source("camera-a")["sources"] == ["camera-a"]

        tus = RiverhogTusClient(
            source="camera-a",
            password="secret",
            base_url="http://testserver",
            allow_insecure_http=True,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        original_http = tus._http  # type: ignore[attr-defined]
        tus._http = TimeoutNeutralTestClient(transport, observer=observer)  # type: ignore[assignment]
        assert (
            tus.put_tus_provenance_journal("upload-1", "journal-1", b"\x1e{}\n")["status"]
            == "accepted"
        )
        assert (
            tus.put_tus_provenance_binding(
                "upload-1",
                {"status": "omitted"},
            )["status"]
            == "accepted"
        )
        assert tus.get_tus_publication("upload-1")["status"] == "pending"
        tus._http = original_http  # type: ignore[assignment]
        tus.close()

        assert (
            transport.get(
                "/internal/tus/auth",
                headers={"Authorization": "Basic fixture"},
            ).status_code
            == 204
        )
        assert (
            transport.post(
                "/internal/tus/hooks",
                headers={"Authorization": "Basic fixture"},
                json={"Type": "pre-create"},
            ).status_code
            == 200
        )

    payload = tmp_path / "wire.bin"
    content = b"tus-wire-qualification"
    payload.write_bytes(content)
    provenance = prepare_file_provenance(
        payload,
        relative_path="wire.bin",
        host_id="qualification-host",
        agent_name="riverhog-operation-qualification",
        agent_version="1",
        omit_reason="Synthetic protocol lifecycle has no source filesystem.",
    )
    operation_ids = {
        "POST": "create_tus_adapter_upload",
        "HEAD": "head_tus_adapter_upload",
        "PATCH": "patch_tus_adapter_upload",
    }

    def wire_handler(request: httpx.Request) -> httpx.Response:
        started = time.perf_counter()
        if request.method == "POST" and request.url.path == "/files/":
            response = httpx.Response(201, headers={"Location": "/files/upload-1"})
        elif request.method == "HEAD" and request.url.path == "/files/upload-1":
            response = httpx.Response(200, headers={"Upload-Offset": "0"})
        elif request.method == "PATCH" and request.url.path == "/files/upload-1":
            response = httpx.Response(
                204,
                headers={"Upload-Offset": str(len(content))},
            )
        elif request.method == "PUT":
            response = httpx.Response(200, json={"status": "accepted"})
        elif request.method == "GET":
            response = httpx.Response(
                200,
                json={
                    "upload_id": "upload-1",
                    "path": "wire.bin",
                    "bytes": len(content),
                    "payload_sha256": hashlib.sha256(content).hexdigest(),
                    "status": "accepted",
                },
            )
        else:
            response = httpx.Response(500)
        if request.method in operation_ids:
            observer.record_external_server(
                operation_ids[request.method],
                elapsed_ms=max((time.perf_counter() - started) * 1000, 0.001),
            )
        return response

    with RiverhogTusClient(
        source="camera-a",
        password="secret",
        base_url="https://adapter.invalid",
        transport=httpx.MockTransport(wire_handler),
    ) as wire:
        receipt = wire.upload_file(
            payload,
            relative_path="wire.bin",
            provenance=provenance,
        )
    assert receipt["status"] == "accepted"

    observer.require(
        {
            "adapter_health_live",
            "adapter_health_ready",
            "get_adapter_status",
            "run_adapter_pass",
            "flush_adapter_source",
            "put_tus_provenance_journal",
            "put_tus_provenance_binding",
            "get_tus_publication",
            "tus_auth",
            "handle_tus_hook",
            *operation_ids.values(),
        }
    )


class _OperatorClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _OperatorClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_adapter_status(self) -> dict[str, object]:
        return _Adapter().status()

    def run_adapter_pass(self) -> dict[str, object]:
        return _Adapter().run_once()

    def flush_adapter_source(self, source_id: str) -> dict[str, object]:
        return _Adapter().flush(source_id)


def test_operator_cli_has_human_and_json_views_for_each_management_operation(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(adapter_app, "RiverhogAdapterClient", _OperatorClient)

    for arguments in (("status",), ("run",), ("flush", "camera-a")):
        assert adapter_app.main([*arguments]) == 0
        human = capsys.readouterr().out
        assert human.strip()

        assert adapter_app.main(["--json", *arguments]) == 0
        payload: dict[str, Any] = json.loads(capsys.readouterr().out)
        assert str(payload["format"]).endswith("/v1")
