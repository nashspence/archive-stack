from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "tus_throughput.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tus_throughput", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tus_throughput_probe_remains_incomplete_and_is_deleted() -> None:
    module = load_script()
    methods: list[str] = []
    patch_sizes: list[int] = []
    offset = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal offset
        methods.append(request.method)
        if request.method == "OPTIONS":
            return httpx.Response(204, headers={"Tus-Extension": "creation,termination"})
        if request.method == "POST":
            assert request.headers["Upload-Length"] == "3145729"
            assert request.headers["Upload-Metadata"].startswith("path ")
            return httpx.Response(201, headers={"Location": "/files/probe"})
        if request.method == "PATCH":
            body = request.read()
            assert int(request.headers["Upload-Offset"]) == offset
            patch_sizes.append(len(body))
            offset += len(body)
            return httpx.Response(204, headers={"Upload-Offset": str(offset)})
        if request.method == "DELETE":
            assert offset == 3 * 1024 * 1024
            return httpx.Response(204)
        raise AssertionError(request.method)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    module._require_termination(client, "https://tus.invalid/files/")
    ticks = iter((10.0, 12.0))
    result = module._measure_probe(
        client,
        "https://tus.invalid/files/",
        size_mib=3,
        chunk_mib=1,
        clock=lambda: next(ticks),
    )

    assert methods == ["OPTIONS", "POST", "PATCH", "PATCH", "PATCH", "DELETE"]
    assert patch_sizes == [1024 * 1024] * 3
    assert result.seconds == 2.0


def test_tus_throughput_probe_is_deleted_after_patch_failure() -> None:
    module = load_script()
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, headers={"Location": "/files/probe"})
        if request.method == "PATCH":
            return httpx.Response(503)
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(request.method)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        module._measure_probe(
            client,
            "https://tus.invalid/files/",
            size_mib=1,
            chunk_mib=1,
        )

    assert methods == ["POST", "PATCH", "DELETE"]


def test_tus_throughput_requires_safe_termination_support() -> None:
    module = load_script()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(204, headers={"Tus-Extension": "creation"})
        )
    )

    with pytest.raises(RuntimeError, match="must support termination"):
        module._require_termination(client, "https://tus.invalid/files/")


def test_tus_throughput_credentials_are_environment_only(monkeypatch) -> None:
    module = load_script()
    monkeypatch.setenv(module.USER_ENV, "camera")
    monkeypatch.setenv(module.PASSWORD_ENV, "example-password")

    assert module._credentials_from_env() == ("camera", "example-password")
    assert os.access(SCRIPT, os.X_OK)
