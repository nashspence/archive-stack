from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

import arc_api.app as arc_app
from arc_api.app import create_app

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi" / "arc.v1.yaml"


def _load_contract_openapi() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


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
