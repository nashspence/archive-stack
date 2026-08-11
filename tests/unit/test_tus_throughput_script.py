from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from tus_transport import TusHttpError

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
    transport = module.TusTransport(client=client, patch_client=client)
    module._require_termination(client, "https://tus.invalid/files/")
    ticks = iter((10.0, 12.0))
    result = module._measure_probe(
        client,
        transport,
        "https://tus.invalid/files/",
        size_mib=3,
        chunk_mib=1,
        clock=lambda: next(ticks),
    )

    assert methods == ["OPTIONS", "POST", "PATCH", "PATCH", "PATCH", "DELETE"]
    assert patch_sizes == [1024 * 1024] * 3
    assert result.http_version == "HTTP/1.1"
    assert result.seconds == 2.0
    transport.close()


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
    transport = module.TusTransport(client=client, patch_client=client)
    with pytest.raises(TusHttpError):
        module._measure_probe(
            client,
            transport,
            "https://tus.invalid/files/",
            size_mib=1,
            chunk_mib=1,
        )

    assert methods == ["POST", "PATCH", "DELETE"]
    transport.close()


def test_tus_throughput_requires_safe_termination_support() -> None:
    module = load_script()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(204, headers={"Tus-Extension": "creation"})
        )
    )

    with pytest.raises(RuntimeError, match="must support termination"):
        module._require_termination(client, "https://tus.invalid/files/")


def test_tus_throughput_resume_probe_confirms_the_authoritative_offset() -> None:
    module = load_script()
    methods: list[str] = []
    offset = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal offset
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, headers={"Location": "/files/probe"})
        if request.method == "PATCH":
            offset += len(request.read())
            return httpx.Response(204, headers={"Upload-Offset": str(offset)})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Upload-Offset": str(offset)})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(request.method)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = module.TusTransport(client=client, patch_client=client)
    ticks = iter((1.0, 2.0))

    result = module._measure_probe(
        client,
        transport,
        "https://tus.invalid/files/",
        size_mib=2,
        chunk_mib=1,
        resume_probe=True,
        clock=lambda: next(ticks),
    )

    assert result.size_mib == 2
    assert methods == ["POST", "PATCH", "HEAD", "PATCH", "DELETE"]
    transport.close()


def test_tus_throughput_batch_reports_aggregate_concurrent_goodput(monkeypatch) -> None:
    module = load_script()
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(204)))
    transport = module.TusTransport(client=client, patch_client=client)

    def measure(*_args, **_kwargs):
        return module.ThroughputResult(
            chunk_mib=1,
            gbit_per_second=0.1,
            http_version="HTTP/1.1",
            mib_per_second=10.0,
            seconds=1.0,
            size_mib=10,
        )

    monkeypatch.setattr(module, "_measure_probe", measure)
    ticks = iter((10.0, 12.0))
    result = module._measure_batch(
        client,
        transport,
        "https://tus.invalid/files/",
        size_mib=10,
        chunk_mib=1,
        uploads=4,
        concurrency=2,
        resume_probe=False,
        clock=lambda: next(ticks),
    )

    assert result.uploads == 4
    assert result.concurrency == 2
    assert result.total_mib == 40
    assert result.mib_per_second == 20.0
    transport.close()


def test_tus_throughput_credentials_are_environment_only(monkeypatch) -> None:
    module = load_script()
    monkeypatch.setenv(module.USER_ENV, "camera")
    monkeypatch.setenv(module.PASSWORD_ENV, "example-password")

    assert module._credentials_from_env() == ("camera", "example-password")
    assert (
        module._authorization_header(("camera", "example-password"))
        == "Basic Y2FtZXJhOmV4YW1wbGUtcGFzc3dvcmQ="
    )
    assert os.access(SCRIPT, os.X_OK)


def test_tus_throughput_result_compares_goodput_to_raw_baseline() -> None:
    module = load_script()
    payload = module._result_payload(
        module.ThroughputResult(
            chunk_mib=64,
            gbit_per_second=0.8,
            http_version="HTTP/1.1",
            mib_per_second=100.0,
            seconds=10.0,
            size_mib=1000,
        ),
        scenario="jeb-munchy",
        workload="large-file",
        baseline_mib_per_second=125.0,
    )

    assert payload["scenario"] == "jeb-munchy"
    assert payload["workload"] == "large-file"
    assert payload["target_utilization"] == 0.8
    assert payload["utilization"] == 0.8
