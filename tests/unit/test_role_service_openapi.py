from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from http_api_contracts import HttpOperationContract
from riverhog_storage_adapter_asgi_support import create_storage_adapter_app
from riverhog_storage_adapter_support import STORAGE_ADAPTER_HTTP_OPERATIONS
from stove0_exiftool_observer.app import create_app as create_exiftool_app
from stove0_ffprobe_sampling_observer.app import create_app as create_ffprobe_app
from stove0_nvenc_av1_opus_review_sampler.app import create_app as create_nvenc_sampler_app
from stove0_nvenc_av1_opus_target.app import create_target_app as create_nvenc_target_app
from stove0_observer_support import OBSERVER_HTTP_OPERATIONS
from stove0_opus_review_sampler.app import create_app as create_opus_sampler_app
from stove0_opus_target.app import create_target_app as create_opus_target_app
from stove0_review_sampler_support import (
    SAMPLER_HTTP_OPERATIONS,
    SamplerHttpBinding,
)
from stove0_review_target.app import create_app as create_review_target_app
from stove0_target_support import TARGET_HTTP_OPERATIONS, TargetHttpBinding, TargetServiceError


def _close() -> None:
    pass


def _applications() -> tuple[
    tuple[FastAPI, tuple[HttpOperationContract, ...], tuple[str, str]], ...
]:
    observer_exif = cast(Any, SimpleNamespace(exiftool="fixture"))
    observer_ffprobe = cast(Any, SimpleNamespace(ffprobe="fixture"))
    target = cast(Any, SimpleNamespace(ffmpeg="fixture", close=_close))
    review = cast(Any, SimpleNamespace(close=_close))
    sampler = cast(Any, SimpleNamespace(ffmpeg="fixture"))
    return (
        (
            create_storage_adapter_app(
                service="fixture-storage-adapter",
                token="secret",
                adapter=cast(Any, object()),
            ),
            STORAGE_ADAPTER_HTTP_OPERATIONS,
            ("GET", "/v1/objects/read"),
        ),
        (
            create_exiftool_app(token="secret", observer=observer_exif),
            OBSERVER_HTTP_OPERATIONS,
            ("POST", "/v1/observer"),
        ),
        (
            create_ffprobe_app(token="secret", observer=observer_ffprobe),
            OBSERVER_HTTP_OPERATIONS,
            ("POST", "/v1/observer"),
        ),
        (
            create_opus_target_app(token="secret", target=target),
            TARGET_HTTP_OPERATIONS,
            ("DELETE", "/v1/target"),
        ),
        (
            create_nvenc_target_app(token="secret", target=target),
            TARGET_HTTP_OPERATIONS,
            ("DELETE", "/v1/target"),
        ),
        (
            create_review_target_app(token="secret", target=review),
            TARGET_HTTP_OPERATIONS,
            ("DELETE", "/v1/target"),
        ),
        (
            create_opus_sampler_app(token="secret", sampler=sampler),
            SAMPLER_HTTP_OPERATIONS,
            ("POST", "/v1/sampler"),
        ),
        (
            create_nvenc_sampler_app(token="secret", sampler=sampler),
            SAMPLER_HTTP_OPERATIONS,
            ("POST", "/v1/sampler"),
        ),
    )


def _operation_set(schema: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        if path.startswith("/v1/")
        for method in path_item
        if method in {"delete", "get", "patch", "post", "put"}
    }


def test_maintained_role_openapi_is_derived_from_each_executable_binding() -> None:
    for app, contracts, _ in _applications():
        schema = app.openapi()
        assert _operation_set(schema) == {
            (contract.method, contract.path) for contract in contracts
        }
        for contract in contracts:
            operation = schema["paths"][contract.path][contract.method.casefold()]
            assert operation["security"] == [{"HTTPBearer": []}]
            if contract.request_kind in {"json", "framed"}:
                request_body = operation["requestBody"]
                assert request_body["required"] is True
                assert "#/$defs/" not in str(request_body)
            success = operation["responses"][str(contract.success_statuses[0])]
            if contract.response_kind == "json":
                assert "application/json" in success["content"]
            elif contract.response_kind == "binary":
                assert "application/octet-stream" in success["content"]
            else:
                assert "content" not in success
            for status in contract.error_statuses:
                expected_codes = {error.code for error in contract.errors if error.status == status}
                assert (
                    set(operation["responses"][str(status)]["x-riverhog-error-codes"])
                    == expected_codes
                )


def test_unadvertised_role_methods_still_receive_runtime_method_rejection() -> None:
    for app, _, (method, path) in _applications():
        with TestClient(app) as client:
            response = client.request(
                method,
                path,
                headers={"Authorization": "Bearer secret"},
            )
        assert response.status_code == 405


def test_target_operation_contract_declares_ordinary_conflict_and_absence() -> None:
    by_operation = {
        (contract.method, contract.path): set(contract.error_statuses)
        for contract in TARGET_HTTP_OPERATIONS
    }

    assert 409 in by_operation[("POST", "/v1/preflight")]
    assert 404 in by_operation[("GET", "/v1/jobs/{job_id}")]
    assert 404 in by_operation[("POST", "/v1/jobs/{job_id}/cancel")]


def test_target_binding_fails_closed_on_an_error_not_declared_for_the_operation() -> None:
    class MisbehavingTarget:
        def get_job(self, _job_id: str) -> None:
            raise TargetServiceError(409, "operation_contract_mismatch", "wrong operation")

    response = TargetHttpBinding(cast(Any, MisbehavingTarget())).handle(
        "GET",
        "/v1/jobs/" + "a" * 64,
    )

    assert response.status == 500
    assert json.loads(response.body)["error"]["code"] == "target_failed"


def test_target_descriptor_validation_failure_is_not_mislabeled_as_a_request_error() -> None:
    class MisbehavingTarget:
        def contract(self) -> None:
            raise ValueError("invalid implementation state")

    response = TargetHttpBinding(cast(Any, MisbehavingTarget())).handle("GET", "/v1/target")

    assert response.status == 500
    assert json.loads(response.body)["error"]["code"] == "target_failed"


def test_sampler_descriptor_validation_failure_is_not_mislabeled_as_a_request_error() -> None:
    class MisbehavingSampler:
        def descriptor(self) -> None:
            raise ValueError("invalid implementation state")

    response = SamplerHttpBinding(cast(Any, MisbehavingSampler())).handle("GET", "/v1/sampler")

    assert response.status == 500
    assert json.loads(response.body)["error"]["code"] == "sampler_failed"


def test_storage_adapter_declares_only_operation_applicable_error_vocabularies() -> None:
    common = {"unauthorized", "invalid_request", "provider_unavailable", "internal_failure"}
    request = {"request_too_large", "invalid_path"}
    by_operation = {
        (contract.method, contract.path): {error.code for error in contract.errors}
        for contract in STORAGE_ADAPTER_HTTP_OPERATIONS
    }

    assert by_operation[("GET", "/v1/adapter")] == common
    assert by_operation[("POST", "/v1/writes/begin")] == common | request
    assert by_operation[("POST", "/v1/writes/complete")] == common | request | {
        "not_found",
        "identity_conflict",
        "integrity_failure",
    }
    assert by_operation[("POST", "/v1/objects/put")] == common | request | {
        "identity_conflict",
        "integrity_failure",
    }
    assert by_operation[("POST", "/v1/objects/read")] == common | request | {
        "not_found",
        "invalid_range",
        "read_not_ready",
        "read_expired",
        "integrity_failure",
    }
    assert by_operation[("POST", "/v1/maintenance/abort-incomplete-writes")] == common | request
