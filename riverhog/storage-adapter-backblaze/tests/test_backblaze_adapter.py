from __future__ import annotations

import os
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from riverhog_storage_adapter_backblaze import app as adapter_app


class _Client:
    def __init__(self) -> None:
        self.ready: list[dict[str, str]] = []

    def head_bucket(self, **request: str) -> None:
        self.ready.append(request)


def test_backblaze_artifact_is_one_immediate_s3_target(
    monkeypatch: Any,
) -> None:
    values = {
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_TOKEN": "adapter-token",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_ENDPOINT_URL": "https://s3.us-west.example.test",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_REGION": "us-west-test",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_BUCKET": "fixture-bucket",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_ACCESS_KEY_ID": "key-id",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_SECRET_ACCESS_KEY": "secret-key",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    client = _Client()
    configs: list[object] = []
    apps: list[object] = []

    def create(config: object, *, tuning: object) -> _Client:
        configs.extend((config, tuning))
        return client

    monkeypatch.setattr(adapter_app, "create_s3_client", create)
    monkeypatch.setattr(
        "riverhog_storage_adapter_backblaze.app.importlib.metadata.version",
        lambda _name: "1.0.0",
    )
    monkeypatch.setattr(
        "riverhog_storage_adapter_backblaze.app.uvicorn.run",
        lambda app, **_kwargs: apps.append(app),
    )

    assert adapter_app.main([]) == 0
    assert len(configs) == 2
    assert len(apps) == 1
    http = TestClient(cast(FastAPI, apps[0]))
    descriptor = http.get(
        "/v1/adapter",
        headers={"Authorization": "Bearer adapter-token"},
    )

    assert descriptor.status_code == 200
    assert descriptor.json()["implementation_id"] == "riverhog.backblaze/v1"
    assert descriptor.json()["read_mode"] == "immediate"
    assert http.get("/health/ready").status_code == 200
    assert client.ready == [{"Bucket": "fixture-bucket"}]
    assert "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_TOKEN" not in os.environ
    assert "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_SECRET_ACCESS_KEY" not in os.environ
