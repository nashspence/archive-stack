from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from fastapi.testclient import TestClient

import arc_api.app as arc_app
from arc_api.app import create_app
from arc_api.deps import ServiceContainer
from arc_api.tus import TUS_CHUNK_CONTENT_TYPE, TUS_RESUMABLE
from arc_core.iso.streaming import IsoStream

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi" / "arc.v1.yaml"


def _load_contract_openapi() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _contract_operation(path: str, method: str) -> dict[str, Any]:
    contract = _load_contract_openapi()
    return contract["paths"][path.removeprefix("/v1")][method]


def _contract_success_headers(path: str, method: str, status_code: str) -> set[str]:
    response = _contract_operation(path, method)["responses"][status_code]
    return {name.casefold() for name in response.get("headers", {})}


def _contract_success_content_types(path: str, method: str, status_code: str) -> set[str]:
    response = _contract_operation(path, method)["responses"][status_code]
    return set(response.get("content", {}))


def _contract_request_headers(path: str, method: str) -> dict[str, dict[str, Any]]:
    operation = _contract_operation(path, method)
    headers = {}
    for parameter in operation.get("parameters", []):
        if parameter.get("in") == "header":
            headers[str(parameter["name"])] = parameter
    return headers


def _contract_required_request_headers(path: str, method: str) -> set[str]:
    headers = _contract_request_headers(path, method)
    return {name for name, parameter in headers.items() if parameter.get("required", False)}


def _contract_request_header_enum(
    path: str,
    method: str,
    header_name: str,
) -> list[str] | None:
    headers = _contract_request_headers(path, method)
    schema = headers.get(header_name, {}).get("schema", {})
    enum = schema.get("enum")
    if enum is None:
        return None
    return list(enum)


def _contract_request_content_types(path: str, method: str) -> set[str]:
    operation = _contract_operation(path, method)
    request_body = operation.get("requestBody", {})
    return set(request_body.get("content", {}))


def _null_schema(schema: object) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null"


def _json_sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _resolve_schema(
    schema: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, Any]:
    if "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    prefix = "#/components/schemas/"
    assert ref.startswith(prefix), ref
    return _resolve_schema(components[ref.removeprefix(prefix)], components)


def _normalize_schema(
    schema: dict[str, Any] | None,
    components: dict[str, Any],
    *,
    include_required: bool,
    include_enums: bool,
) -> dict[str, Any] | None:
    if schema is None:
        return None
    schema = _resolve_schema(schema, components)
    for nullable_key in ("anyOf", "oneOf"):
        if nullable_key not in schema:
            continue
        variants = [_resolve_schema(variant, components) for variant in schema[nullable_key]]
        non_null = [variant for variant in variants if not _null_schema(variant)]
        if len(non_null) == 1 and len(variants) == 2:
            return _normalize_schema(
                non_null[0],
                components,
                include_required=include_required,
                include_enums=include_enums,
            )

    for key in ("allOf", "anyOf", "oneOf"):
        if key not in schema:
            continue
        variants = [
            _normalize_schema(
                item,
                components,
                include_required=include_required,
                include_enums=include_enums,
            )
            for item in schema[key]
        ]
        object_variants = [
            variant
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") == "object"
        ]
        if object_variants and len(object_variants) == len(variants):
            merged_properties: dict[str, Any] = {}
            for variant in object_variants:
                merged_properties.update(variant.get("properties", {}))
            normalized: dict[str, Any] = {
                "type": "object",
                "properties": dict(sorted(merged_properties.items())),
            }
            if include_required:
                required_sets = [set(variant.get("required", [])) for variant in object_variants]
                normalized["required"] = (
                    sorted(set.intersection(*required_sets)) if required_sets else []
                )
            return normalized
        return {"variants": sorted(variants, key=_json_sort_key)}

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = sorted(t for t in schema_type if t != "null")
        if non_null_types:
            schema_type = non_null_types[0] if len(non_null_types) == 1 else non_null_types

    if schema_type == "object" or "properties" in schema:
        normalized = {
            "type": "object",
            "properties": {
                name: _normalize_schema(
                    property_schema,
                    components,
                    include_required=include_required,
                    include_enums=include_enums,
                )
                for name, property_schema in sorted(schema.get("properties", {}).items())
            },
        }
        if include_required and "required" in schema:
            normalized["required"] = sorted(schema["required"])
        return normalized

    if schema_type == "array" or "items" in schema:
        return {
            "type": "array",
            "items": _normalize_schema(
                schema.get("items"),
                components,
                include_required=include_required,
                include_enums=include_enums,
            ),
        }

    normalized = {}
    if schema_type is not None:
        normalized["type"] = schema_type
    if include_enums and "enum" in schema:
        normalized["enum"] = list(schema["enum"])
    return normalized or {"type": "any"}


def _normalize_parameters(
    parameters: list[dict[str, Any]] | None,
    components: dict[str, Any],
) -> list[dict[str, Any]]:
    if not parameters:
        return []
    normalized = []
    for parameter in parameters:
        if parameter.get("in") not in {"path", "query"}:
            continue
        normalized.append(
            {
                "in": parameter["in"],
                "name": parameter["name"],
                "required": parameter.get("required", False),
                "schema": _normalize_schema(
                    parameter.get("schema"),
                    components,
                    include_required=False,
                    include_enums=True,
                ),
            }
        )
    return sorted(normalized, key=lambda item: (item["in"], item["name"]))


def _normalize_json_body(
    body: dict[str, Any] | None,
    components: dict[str, Any],
) -> dict[str, Any] | None:
    if not body:
        return None
    content = body.get("content", {})
    json_content = content.get("application/json")
    if json_content is None:
        return None
    return {
        "required": body.get("required", False),
        "schema": _normalize_schema(
            json_content.get("schema"),
            components,
            include_required=True,
            include_enums=True,
        ),
    }


def _normalize_success_json_responses(
    responses: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for status_code, response in responses.items():
        if not status_code.startswith("2"):
            continue
        content = response.get("content", {})
        json_content = content.get("application/json")
        if json_content is None:
            continue
        normalized_schema = _normalize_schema(
            json_content.get("schema"),
            components,
            include_required=False,
            include_enums=False,
        )
        if normalized_schema == {"type": "any"}:
            continue
        normalized[status_code] = normalized_schema
    return normalized


def _normalize_operations(spec: dict[str, Any], *, prefix_paths: bool) -> dict[str, Any]:
    components = spec.get("components", {}).get("schemas", {})
    base_path = ""
    if prefix_paths:
        servers = spec.get("servers", [])
        base_path = str(servers[0]["url"]) if servers else ""
    # FastAPI's generated OpenAPI remains useful for route and JSON-shape drift, but
    # it still emits noise we intentionally normalize away here:
    # - autogenerated tags / operation ids
    # - implicit validation responses
    # - missing path params on OPTIONS operations
    # Request headers / non-JSON request bodies and response headers / non-JSON
    # success responses are enforced separately by the contract-driven runtime
    # assertions below.
    parameterless_methods = {"options"}
    normalized: dict[str, Any] = {}
    for path, path_item in sorted(spec["paths"].items()):
        full_path = f"{base_path}{path}"
        methods = {
            method: {
                "parameters": (
                    []
                    if method in parameterless_methods
                    else _normalize_parameters(operation.get("parameters"), components)
                ),
                "request_body": _normalize_json_body(operation.get("requestBody"), components),
                "responses": _normalize_success_json_responses(
                    operation.get("responses", {}),
                    components,
                ),
            }
            for method, operation in sorted(path_item.items())
        }
        normalized[full_path] = methods
    return normalized


def test_live_openapi_matches_checked_in_contract_shape() -> None:
    contract = _load_contract_openapi()
    actual = create_app().openapi()
    assert _normalize_operations(contract, prefix_paths=True) == _normalize_operations(
        actual,
        prefix_paths=False,
    )


class _StubCollectionUploads:
    def expire_stale_uploads(self) -> None:
        return None

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        return {
            "path": path,
            "protocol": "tus",
            "upload_url": f"https://uploads.test/collections/{collection_id}/{path}",
            "offset": 0,
            "length": 12,
            "checksum_algorithm": "sha256",
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        assert collection_id
        assert path
        assert checksum
        assert content
        return {
            "offset": offset + len(content),
            "length": offset + len(content),
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def get_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        assert collection_id
        assert path
        return {
            "offset": 6,
            "length": 12,
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def cancel_file_upload(self, collection_id: str, path: str) -> None:
        assert collection_id
        assert path


class _StubFetchUploads:
    def expire_stale_uploads(self) -> None:
        return None

    def create_or_resume_upload(self, *, fetch_id: str, entry_id: str) -> dict[str, object]:
        return {
            "entry": entry_id,
            "protocol": "tus",
            "upload_url": f"https://uploads.test/fetches/{fetch_id}/{entry_id}",
            "offset": 0,
            "length": 12,
            "checksum_algorithm": "sha256",
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        assert fetch_id
        assert entry_id
        assert checksum
        assert content
        return {
            "offset": offset + len(content),
            "length": offset + len(content),
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def get_entry_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        assert fetch_id
        assert entry_id
        return {
            "offset": 6,
            "length": 12,
            "expires_at": "2026-04-28T00:00:00Z",
        }

    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None:
        assert fetch_id
        assert entry_id


class _StubFiles:
    def get_content(self, target: str) -> bytes:
        assert target
        return b"fixture file bytes"


class _StubPlanning:
    async def get_iso_stream(self, image_id: str) -> IsoStream:
        assert image_id

        async def body() -> AsyncIterator[bytes]:
            yield b"fixture iso bytes"

        return IsoStream(
            body=body(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="fixture.iso"',
                "Cache-Control": "no-store",
            },
        )


class _StubGlacierUploads:
    def process_due_uploads(self, *, limit: int) -> None:
        assert limit >= 0


class _StubGlacierReporting:
    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> dict[str, object]:
        _ = image_id, collection
        return {}


class _StubRecoverySessions:
    def get(self, session_id: str) -> dict[str, object]:
        _ = session_id
        return {}

    def get_for_image(self, image_id: str) -> dict[str, object]:
        _ = image_id
        return {}

    def create_or_resume_for_image(self, image_id: str) -> dict[str, object]:
        _ = image_id
        return {}

    def approve(self, session_id: str) -> dict[str, object]:
        _ = session_id
        return {}

    def complete(self, session_id: str) -> dict[str, object]:
        _ = session_id
        return {}

    def process_due_sessions(self, *, limit: int) -> None:
        assert limit >= 0


def _contract_runtime_client() -> TestClient:
    container = ServiceContainer(
        collections=_StubCollectionUploads(),  # type: ignore[arg-type]
        search=SimpleNamespace(),
        planning=_StubPlanning(),  # type: ignore[arg-type]
        glacier_uploads=_StubGlacierUploads(),  # type: ignore[arg-type]
        glacier_reporting=_StubGlacierReporting(),  # type: ignore[arg-type]
        recovery_sessions=_StubRecoverySessions(),  # type: ignore[arg-type]
        copies=SimpleNamespace(),
        pins=SimpleNamespace(),
        fetches=_StubFetchUploads(),  # type: ignore[arg-type]
        files=_StubFiles(),  # type: ignore[arg-type]
    )
    app = create_app(
        container=container,
        upload_expiry_reaper_interval=3600,
        glacier_upload_reaper_interval=3600,
    )
    return TestClient(app)


def _response_header_names(response: Any) -> set[str]:
    return {name.casefold() for name in response.headers}


def _assert_contract_success_headers(
    response: Any,
    *,
    contract_path: str,
    method: str,
    status_code: str,
) -> None:
    expected = _contract_success_headers(contract_path, method, status_code)
    assert expected.issubset(_response_header_names(response))


def _assert_contract_success_content_type(
    response: Any,
    *,
    contract_path: str,
    method: str,
    status_code: str,
) -> None:
    expected = _contract_success_content_types(contract_path, method, status_code)
    assert len(expected) == 1
    assert response.headers["content-type"].split(";", 1)[0] == next(iter(expected))


def _valid_tus_chunk_headers(*, offset: str = "0") -> dict[str, str]:
    return {
        "Content-Type": TUS_CHUNK_CONTENT_TYPE,
        "Tus-Resumable": TUS_RESUMABLE,
        "Upload-Offset": offset,
        "Upload-Checksum": "sha256 deadbeef",
    }


def _assert_bad_request(response: Any) -> None:
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def _assert_contract_patch_request_requirements(
    client: TestClient,
    *,
    runtime_path: str,
    contract_path: str,
) -> None:
    expected_headers = _contract_required_request_headers(contract_path, "patch")
    assert expected_headers == {"Tus-Resumable", "Upload-Offset", "Upload-Checksum"}

    expected_content_types = _contract_request_content_types(contract_path, "patch")
    assert expected_content_types == {TUS_CHUNK_CONTENT_TYPE}

    valid = client.patch(
        runtime_path,
        headers=_valid_tus_chunk_headers(),
        content=b"chunk-bytes",
    )
    assert valid.status_code == 204

    for missing_header in sorted(expected_headers):
        headers = _valid_tus_chunk_headers()
        del headers[missing_header]
        missing = client.patch(
            runtime_path,
            headers=headers,
            content=b"chunk-bytes",
        )
        _assert_bad_request(missing)

    tus_values = _contract_request_header_enum(contract_path, "patch", "Tus-Resumable")
    assert tus_values == [TUS_RESUMABLE]
    invalid_tus = client.patch(
        runtime_path,
        headers={**_valid_tus_chunk_headers(), "Tus-Resumable": "0.9.0"},
        content=b"chunk-bytes",
    )
    _assert_bad_request(invalid_tus)

    wrong_content_type = client.patch(
        runtime_path,
        headers={**_valid_tus_chunk_headers(), "Content-Type": "application/json"},
        content=b"chunk-bytes",
    )
    _assert_bad_request(wrong_content_type)

    missing_content_type_headers = _valid_tus_chunk_headers()
    del missing_content_type_headers["Content-Type"]
    missing_content_type = client.patch(
        runtime_path,
        headers=missing_content_type_headers,
        content=b"chunk-bytes",
    )
    _assert_bad_request(missing_content_type)


def test_collection_upload_runtime_matches_contract_headers() -> None:
    with _contract_runtime_client() as client:
        runtime_path = "/v1/collection-uploads/docs/files/report.txt/upload"
        contract_path = "/v1/collection-uploads/{collection_id}/files/{path}/upload"

        post = client.post(runtime_path)
        assert post.status_code == 200
        _assert_contract_success_headers(
            post,
            contract_path=contract_path,
            method="post",
            status_code="200",
        )

        patch = client.patch(
            runtime_path,
            headers=_valid_tus_chunk_headers(),
            content=b"chunk-bytes",
        )
        assert patch.status_code == 204
        _assert_contract_success_headers(
            patch,
            contract_path=contract_path,
            method="patch",
            status_code="204",
        )

        head = client.head(runtime_path)
        assert head.status_code == 204
        _assert_contract_success_headers(
            head,
            contract_path=contract_path,
            method="head",
            status_code="204",
        )

        delete = client.delete(runtime_path)
        assert delete.status_code == 204
        _assert_contract_success_headers(
            delete,
            contract_path=contract_path,
            method="delete",
            status_code="204",
        )

        options = client.options(runtime_path)
        assert options.status_code == 204
        _assert_contract_success_headers(
            options,
            contract_path=contract_path,
            method="options",
            status_code="204",
        )


def test_fetch_upload_runtime_matches_contract_headers() -> None:
    with _contract_runtime_client() as client:
        runtime_path = "/v1/fetches/fx-1/entries/e1/upload"
        contract_path = "/v1/fetches/{fetch_id}/entries/{entry_id}/upload"

        post = client.post(runtime_path)
        assert post.status_code == 200
        _assert_contract_success_headers(
            post,
            contract_path=contract_path,
            method="post",
            status_code="200",
        )

        patch = client.patch(
            runtime_path,
            headers=_valid_tus_chunk_headers(),
            content=b"chunk-bytes",
        )
        assert patch.status_code == 204
        _assert_contract_success_headers(
            patch,
            contract_path=contract_path,
            method="patch",
            status_code="204",
        )

        head = client.head(runtime_path)
        assert head.status_code == 204
        _assert_contract_success_headers(
            head,
            contract_path=contract_path,
            method="head",
            status_code="204",
        )

        delete = client.delete(runtime_path)
        assert delete.status_code == 204
        _assert_contract_success_headers(
            delete,
            contract_path=contract_path,
            method="delete",
            status_code="204",
        )

        options = client.options(runtime_path)
        assert options.status_code == 204
        _assert_contract_success_headers(
            options,
            contract_path=contract_path,
            method="options",
            status_code="204",
        )


def test_collection_upload_runtime_enforces_contract_patch_request_requirements() -> None:
    with _contract_runtime_client() as client:
        _assert_contract_patch_request_requirements(
            client,
            runtime_path="/v1/collection-uploads/docs/files/report.txt/upload",
            contract_path="/v1/collection-uploads/{collection_id}/files/{path}/upload",
        )


def test_fetch_upload_runtime_enforces_contract_patch_request_requirements() -> None:
    with _contract_runtime_client() as client:
        _assert_contract_patch_request_requirements(
            client,
            runtime_path="/v1/fetches/fx-1/entries/e1/upload",
            contract_path="/v1/fetches/{fetch_id}/entries/{entry_id}/upload",
        )


def test_binary_download_runtime_matches_contract_content_types() -> None:
    with _contract_runtime_client() as client:
        iso = client.get("/v1/images/20260420T040001Z/iso")
        assert iso.status_code == 200
        assert iso.content == b"fixture iso bytes"
        _assert_contract_success_content_type(
            iso,
            contract_path="/v1/images/{image_id}/iso",
            method="get",
            status_code="200",
        )

        file_content = client.get("/v1/files/docs/tax/2022/invoice-123.pdf/content")
        assert file_content.status_code == 200
        assert file_content.content == b"fixture file bytes"
        _assert_contract_success_content_type(
            file_content,
            contract_path="/v1/files/{target}/content",
            method="get",
            status_code="200",
        )


def test_healthz_is_available_and_hidden_from_openapi() -> None:
    app = create_app()
    client = TestClient(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    data = client.get("/openapi.json").json()
    assert "/healthz" not in data["paths"]
    assert "/_test/restart" not in data["paths"]


def test_restart_control_route_is_disabled_by_default() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/restart")
    assert response.status_code == 404


def test_restart_control_route_is_available_when_enabled(monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setenv("ARC_ENABLE_TEST_CONTROL", "1")
    monkeypatch.setattr(arc_app, "_terminate_for_restart", lambda: called.append("restart"))

    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/restart")
    assert response.status_code == 202
    assert response.json()["status"] == "restarting"
    assert called == ["restart"]


def test_reset_control_route_is_available_when_enabled(monkeypatch) -> None:
    called: list[str] = []

    monkeypatch.setenv("ARC_ENABLE_TEST_CONTROL", "1")
    monkeypatch.setattr(arc_app, "_reset_runtime_state", lambda: called.append("reset"))

    app = create_app()
    client = TestClient(app)

    response = client.post("/_test/reset")
    assert response.status_code == 204
    assert called == ["reset"]
