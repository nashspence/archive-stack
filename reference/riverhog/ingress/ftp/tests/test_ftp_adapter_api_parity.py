from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from riverhog_ftp_adapter import app as adapter_app
from riverhog_ftp_adapter.app import FtpAdapterComposition, create_app
from riverhog_ftp_adapter.config import FtpAdapterConfig, SourceConfig
from riverhog_ftp_adapter_api_client import RiverhogFtpAdapterClient

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient


class _Riverhog:
    def close(self) -> None:
        pass

    def list_archive_stores(
        self, *, page_size: int, page_token: str | None = None
    ) -> dict[str, object]:
        assert (page_size, page_token) == (1, None)
        return {"stores": [], "page_size": 1, "next_page_token": None}


class _Adapter:
    def status(self) -> dict[str, object]:
        return {
            "format": "riverhog-ftp-adapter-status/v1",
            "sources": [{"id": "camera-a", "claims": 0, "claim_bytes": 0}],
        }

    def run_once(self) -> dict[str, object]:
        return {
            "format": "riverhog-ftp-adapter-pass/v1",
            "completed": 0,
            "failed": [],
            "sources": ["camera-a"],
        }

    def flush(self, source_id: str) -> dict[str, object]:
        assert source_id == "camera-a"
        return {
            "format": "riverhog-ftp-adapter-pass/v1",
            "completed": 0,
            "failed": [],
            "sources": [source_id],
        }


def _composition(tmp_path: Path) -> FtpAdapterComposition:
    config = FtpAdapterConfig(
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        riverhog_base_url="https://riverhog.invalid",
        riverhog_token="riverhog-token",
        api_token="adapter-token",
        sources=(
            SourceConfig(
                id="camera-a",
                root=tmp_path / "landing",
                ingest_source="ftp:camera-a",
                provenance="omit",
                provenance_omission_reason="Fixture has no host provenance.",
            ),
        ),
    )
    return FtpAdapterComposition(config, _Riverhog(), _Adapter())  # type: ignore[arg-type]


def test_public_openapi_has_one_stable_operation_for_each_official_client_method(
    tmp_path: Path,
) -> None:
    schema = create_app(_composition(tmp_path)).openapi()
    operations = {
        operation["operationId"]
        for path, item in schema["paths"].items()
        if path.startswith("/v1/")
        for method, operation in item.items()
        if method in {"get", "post"}
    }

    assert operations == {
        "flush_ftp_adapter_source",
        "get_ftp_adapter_status",
        "run_ftp_adapter_pass",
    }
    assert {"ErrorResponse", "HealthResponse"} <= set(schema["components"]["schemas"])
    assert all(
        "422" not in operation["responses"]
        for item in schema["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "responses" in operation
    )
    assert operations <= set(dir(RiverhogFtpAdapterClient))


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

    with RiverhogFtpAdapterClient(
        "https://adapter.invalid",
        "adapter-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get_ftp_adapter_status()
        client.run_ftp_adapter_pass()
        client.flush_ftp_adapter_source("camera/a")

    assert requests == [
        ("GET", "/v1/status"),
        ("POST", "/v1/run"),
        ("POST", "/v1/sources/camera%2Fa/flush"),
    ]


def test_official_client_positive_disposable_lifecycle(tmp_path: Path) -> None:
    application = create_app(_composition(tmp_path))
    observer = OperationObserver.install(application, application="riverhog-ftp-adapter")

    with TestClient(
        application,
        headers={"Authorization": "Bearer adapter-token"},
    ) as transport:
        client = RiverhogFtpAdapterClient(
            "http://testserver",
            "adapter-token",
            allow_insecure_http=True,
        )
        client._http.close()  # type: ignore[attr-defined]
        client._http = TimeoutNeutralTestClient(transport, observer=observer)  # type: ignore[assignment]
        assert client.ftp_adapter_health_live().status == "ok"
        assert client.ftp_adapter_health_ready().status == "ok"
        assert client.get_ftp_adapter_status()["format"] == "riverhog-ftp-adapter-status/v1"
        assert client.run_ftp_adapter_pass()["format"] == "riverhog-ftp-adapter-pass/v1"
        assert client.flush_ftp_adapter_source("camera-a")["sources"] == ["camera-a"]
        client.close()

    observer.require(
        {
            "ftp_adapter_health_live",
            "ftp_adapter_health_ready",
            "get_ftp_adapter_status",
            "run_ftp_adapter_pass",
            "flush_ftp_adapter_source",
        }
    )


class _OperatorClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _OperatorClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_ftp_adapter_status(self) -> dict[str, object]:
        return _Adapter().status()

    def run_ftp_adapter_pass(self) -> dict[str, object]:
        return _Adapter().run_once()

    def flush_ftp_adapter_source(self, source_id: str) -> dict[str, object]:
        return _Adapter().flush(source_id)


def test_operator_cli_has_human_and_json_views_for_each_management_operation(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    monkeypatch.setattr(adapter_app, "RiverhogFtpAdapterClient", _OperatorClient)

    for arguments in (("status",), ("run",), ("flush", "camera-a")):
        assert adapter_app.main([*arguments]) == 0
        assert capsys.readouterr().out.strip()

        assert adapter_app.main(["--json", *arguments]) == 0
        payload: dict[str, Any] = json.loads(capsys.readouterr().out)
        assert str(payload["format"]).endswith("/v1")
