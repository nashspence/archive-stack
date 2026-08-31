#!/usr/bin/env python3
"""Generate the normalized v1 external-extent decision projection."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = "riverhog-extent-contract/v1"
EXTENT_DECLARATION = "x-riverhog-extent"
PRINCIPLES: dict[str, object] = {
    "logical_totals": (
        "A finite logical total has no product-level semantic maximum unless its owning "
        "contract declares one."
    ),
    "bounded_work": (
        "Large logical totals cross bounded pages, segments, or restartable work steps; a "
        "carrier bound does not redefine the logical total."
    ),
    "operational_capacity": (
        "Capacity policy may explicitly reject, defer, or throttle work, but must not silently "
        "truncate it or become an undocumented semantic ceiling."
    ),
    "configuration": (
        "Hardware- or environment-dependent limits that observably affect accepted work are "
        "operator-configurable and source-linked."
    ),
    "implementation_privacy": (
        "Buffers, provider mechanics, database layout, and other non-observable implementation "
        "extents are not frozen here."
    ),
}
RULES: dict[str, dict[str, object]] = {
    "schema-bound/v1": {
        "policy": "fixed-or-contract-max",
        "authority": "the projected JSON Schema constraint",
        "exceeded": "schema-validation-error",
        "requirement": "a non-fixed set maximum carries an owning reason declaration",
    },
    "bounded-segment/v1": {
        "policy": "segmented_no_total_max",
        "authority": "the owning schema's x-riverhog-extent declaration",
        "semantic_maximum": None,
        "exceeded": "bounded-carrier-validation-error",
        "completion": "the owner-declared progression or repeated-work contract",
    },
    "route-progression/v1": {
        "policy": "segmented_no_total_max",
        "authority": "the route-owned x-riverhog-read-collection declaration",
        "completion": "the owning progression contract",
    },
    "extension-contract/v1": {
        "policy": "extension_owned",
        "authority": "the independently versioned extension contract",
        "core_semantic_maximum": None,
    },
    "no-semantic-maximum/v1": {
        "policy": "operational_policy",
        "authority": "the owning schema's deliberate absence of a semantic maximum",
        "semantic_maximum": None,
        "declared_operational_maximum": None,
        "future_capacity_behavior": "explicit-configured-reject-defer-or-throttle",
        "hidden_maximum": "forbidden",
        "silent_truncation": "forbidden",
    },
    "configuration-composition/v1": {
        "policy": "operational_policy",
        "authority": "the owning validated deployment configuration document",
        "semantic_maximum": None,
        "declared_operational_maximum": None,
        "hidden_maximum": "forbidden",
        "silent_truncation": "forbidden",
    },
    "configured-capacity/v1": {
        "policy": "operational_policy",
        "authority": "the source-linked operator configuration field",
        "capacity_behavior": "explicit-reject-defer-or-throttle",
        "silent_truncation": "forbidden",
    },
}
POLICIES = frozenset(
    {
        "fixed",
        "contract_max",
        "segmented_no_total_max",
        "extension_owned",
        "operational_policy",
    }
)
_SCHEMA_BRANCHES = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_SINGLE_CHILDREN = (
    "items",
    "additionalProperties",
    "if",
    "then",
    "else",
    "not",
    "contains",
    "propertyNames",
    "unevaluatedProperties",
)
_EXTENT_NAME = re.compile(
    r"(?:^|_)(?:age|bytes|concurrency|count|depth|duration|entries|files?|interval|"
    r"items?|lease|length|limit|members?|offset|ordinal|outputs?|parts?|retention|"
    r"segments?|seconds|size|subjects?|timeout|ttl|windows?)(?:$|_)",
    re.IGNORECASE,
)
_CONFIGURATION_EXTENT_NAME = re.compile(
    r"(?:^|_)(?:age|bytes|concurrency|count|depth|duration|entries|interval|lease|"
    r"limit|max|retention|seconds|size|timeout|ttl|window)(?:$|_)",
    re.IGNORECASE,
)
_FIXED_WIDTH_PATTERN = re.compile(r"^\^\[[^]]+\]\{([1-9][0-9]*)\}\$$")
_GENERATED_PROTOCOL_OWNERS = {
    "generated:riverhog-storage-adapter": "riverhog-storage-adapter-protocol",
    "generated:stove0-observer": "stove0-observer-protocol",
    "generated:stove0-review-sampler": "stove0-review-sampler-protocol",
    "generated:stove0-target": "stove0-target-protocol",
}


class ExtentContractError(RuntimeError):
    """The generated extent decision surface is incomplete or contradictory."""


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer(*parts: str) -> str:
    return "/" + "/".join(_escape(part) for part in parts)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _schema_children(schema: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for keyword in ("$defs", "definitions", "properties", "patternProperties", "schemas"):
        children = schema.get(keyword)
        if isinstance(children, Mapping):
            for name, child in sorted(children.items()):
                if isinstance(child, Mapping):
                    yield f"/{_escape(keyword)}/{_escape(str(name))}", child
    for keyword in _SCHEMA_SINGLE_CHILDREN:
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            yield f"/{keyword}", child
    for keyword in _SCHEMA_BRANCHES:
        children = schema.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    yield f"/{keyword}/{index}", child


def _walk_schema(
    schema: Mapping[str, Any],
    *,
    pointer: str,
    include_definitions: bool = True,
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    yield pointer, schema
    for suffix, child in _schema_children(schema):
        if not include_definitions and suffix.startswith(("/$defs/", "/definitions/")):
            continue
        yield from _walk_schema(
            child,
            pointer=f"{pointer}{suffix}",
            include_definitions=include_definitions,
        )


def _named_definitions(
    schema: Mapping[str, Any],
    *,
    pointer: str,
) -> dict[str, tuple[Mapping[str, Any], str]]:
    found: dict[str, tuple[Mapping[str, Any], str]] = {}

    def visit(current: Mapping[str, Any], current_pointer: str) -> None:
        for keyword in ("$defs", "definitions"):
            definitions = current.get(keyword)
            if not isinstance(definitions, Mapping):
                continue
            for name, definition in sorted(definitions.items()):
                if not isinstance(definition, Mapping):
                    continue
                definition_pointer = f"{current_pointer}/{_escape(keyword)}/{_escape(str(name))}"
                existing = found.get(str(name))
                if existing is not None and existing[0] != definition:
                    raise ExtentContractError(f"named schema definition is contradictory: {name}")
                if existing is None or definition_pointer < existing[1]:
                    found[str(name)] = (definition, definition_pointer)
                visit(definition, definition_pointer)
        for suffix, child in _schema_children(current):
            if suffix.startswith(("/$defs/", "/definitions/")):
                continue
            visit(child, f"{current_pointer}{suffix}")

    visit(schema, pointer)
    return found


def _direct_response_array_policies(
    openapi: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, object]]:
    """Bind a page response's direct item array to its route-owned progression contract."""

    result: dict[tuple[str, str], dict[str, object]] = {}
    schemas = openapi.get("components", {}).get("schemas", {})
    if not isinstance(schemas, Mapping):
        return result
    for path_item in openapi.get("paths", {}).values():
        if not isinstance(path_item, Mapping):
            continue
        for operation in path_item.values():
            if not isinstance(operation, Mapping):
                continue
            read = operation.get("x-riverhog-read-collection")
            if not isinstance(read, Mapping):
                continue
            response = operation.get("responses", {}).get("200", {})
            if not isinstance(response, Mapping):
                continue
            content = response.get("content", {})
            if not isinstance(content, Mapping):
                continue
            response_schema = next(
                (
                    value.get("schema")
                    for value in content.values()
                    if isinstance(value, Mapping) and isinstance(value.get("schema"), Mapping)
                ),
                None,
            )
            if not isinstance(response_schema, Mapping):
                continue
            reference = response_schema.get("$ref")
            if not isinstance(reference, str) or not reference.startswith("#/components/schemas/"):
                continue
            model = reference.rsplit("/", 1)[-1]
            model_schema = schemas.get(model)
            if not isinstance(model_schema, Mapping):
                continue
            properties = model_schema.get("properties", {})
            if not isinstance(properties, Mapping):
                continue
            direct_arrays = [
                str(name)
                for name, value in properties.items()
                if isinstance(value, Mapping) and value.get("type") == "array"
            ]
            if len(direct_arrays) != 1:
                continue
            result[(model, direct_arrays[0])] = dict(read)
    return result


def _source_owner(section: str, authority: str) -> tuple[str, bool]:
    if section == "http_openapi":
        return authority, False
    if section == "configuration_documents":
        return authority, False
    platform_observer_schema = (
        "/provenance/observers/schemas/" in authority
        and not authority.endswith(("/observation-policy.json", "/sparse-map.json"))
    )
    generated_owner = _GENERATED_PROTOCOL_OWNERS.get(authority)
    if generated_owner is not None:
        return generated_owner, False
    if authority.startswith("generated:"):
        raise ExtentContractError(f"generated protocol has no extent owner: {authority}")
    if platform_observer_schema:
        return authority, True
    return authority, False


def _bound_decision(
    *,
    identity: str,
    owner: str,
    source_pointer: str,
    dimension: str,
    unit: str,
    minimum: int | float | None,
    maximum: int | float,
    reason: str | None = None,
) -> dict[str, object]:
    fixed = minimum is not None and minimum == maximum
    decision: dict[str, object] = {
        "id": identity,
        "owner": owner,
        "source_pointer": source_pointer,
        "dimension": dimension,
        "unit": unit,
        "policy": "fixed" if fixed else "contract_max",
        "rule": "schema-bound/v1",
        "reason": reason or ("fixed-public-representation" if fixed else "schema-maximum"),
        "maximum": maximum,
    }
    if minimum is not None:
        decision["minimum"] = minimum
    return decision


def _extent_declaration(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    declaration = schema.get(EXTENT_DECLARATION)
    if declaration is None:
        return None
    if not isinstance(declaration, Mapping):
        raise ExtentContractError(f"{EXTENT_DECLARATION} must be an object")
    return declaration


def _open_extent_decision(
    *,
    identity: str,
    owner: str,
    source_pointer: str,
    dimension: str,
    unit: str,
    extension_owned: bool,
    configuration_document: bool,
) -> dict[str, object]:
    if extension_owned:
        return {
            "id": identity,
            "owner": owner,
            "source_pointer": source_pointer,
            "dimension": dimension,
            "unit": unit,
            "policy": "extension_owned",
            "rule": "extension-contract/v1",
            "reason": "independently-versioned-extension-authority",
            "maximum": None,
        }
    rule = "configuration-composition/v1" if configuration_document else "no-semantic-maximum/v1"
    return {
        "id": identity,
        "owner": owner,
        "source_pointer": source_pointer,
        "dimension": dimension,
        "unit": unit,
        "policy": "operational_policy",
        "rule": rule,
        "reason": (
            "validated-deployment-composition"
            if configuration_document
            else "no-declared-semantic-maximum"
        ),
        "maximum": None,
        "capacity_authority": {
            "owner": owner,
            "declared_maximum": None,
            "hidden_maximum": "forbidden",
        },
    }


def _declared_cardinality_decision(
    *,
    identity: str,
    owner: str,
    source_pointer: str,
    schema: Mapping[str, Any],
    maximum_keyword: str,
    minimum_keyword: str,
    unit: str,
) -> dict[str, object] | None:
    maximum = schema.get(maximum_keyword)
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        return None
    minimum = schema.get(minimum_keyword)
    fixed = isinstance(minimum, int) and not isinstance(minimum, bool) and minimum == maximum
    declaration = _extent_declaration(schema)
    if fixed:
        return _bound_decision(
            identity=identity,
            owner=owner,
            source_pointer=source_pointer,
            dimension="cardinality",
            unit=unit,
            minimum=minimum,
            maximum=maximum,
            reason="fixed-public-representation",
        )
    if declaration is None and maximum == 0:
        return _bound_decision(
            identity=identity,
            owner=owner,
            source_pointer=source_pointer,
            dimension="cardinality",
            unit=unit,
            minimum=minimum if isinstance(minimum, int) else None,
            maximum=maximum,
            reason="state-conditioned-empty-set",
        )
    if declaration is None:
        raise ExtentContractError(
            f"set maximum has no {EXTENT_DECLARATION} reason: {source_pointer}"
        )
    policy = declaration.get("policy")
    reason = declaration.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ExtentContractError(f"set maximum has no reason: {source_pointer}")
    if policy == "contract_max":
        return _bound_decision(
            identity=identity,
            owner=owner,
            source_pointer=source_pointer,
            dimension="cardinality",
            unit=unit,
            minimum=minimum if isinstance(minimum, int) else None,
            maximum=maximum,
            reason=reason,
        )
    if policy != "segmented_no_total_max":
        raise ExtentContractError(f"set maximum has invalid declared policy: {source_pointer}")
    return {
        "id": identity,
        "owner": owner,
        "source_pointer": source_pointer,
        "dimension": "cardinality",
        "unit": unit,
        "policy": "segmented_no_total_max",
        "rule": "bounded-segment/v1",
        "reason": reason,
        "minimum": minimum if isinstance(minimum, int) else None,
        "maximum": maximum,
        "progression": {
            key: value for key, value in declaration.items() if key not in {"policy", "reason"}
        },
    }


def _array_decision(
    *,
    identity: str,
    owner: str,
    source_pointer: str,
    schema: Mapping[str, Any],
    extension_owned: bool,
    configuration_document: bool,
    read_policy: Mapping[str, object] | None,
) -> dict[str, object]:
    maximum = schema.get("maxItems")
    minimum = schema.get("minItems")
    if read_policy is not None:
        decision: dict[str, object] = {
            "id": identity,
            "owner": owner,
            "source_pointer": source_pointer,
            "dimension": "cardinality",
            "unit": "items",
            "policy": "segmented_no_total_max",
            "rule": "route-progression/v1",
            "reason": "bounded-route-page",
            **({"maximum": maximum} if isinstance(maximum, int) else {}),
            **({"minimum": minimum} if isinstance(minimum, int) else {}),
            "progression": dict(read_policy),
        }
        return decision
    declared = _declared_cardinality_decision(
        identity=identity,
        owner=owner,
        source_pointer=source_pointer,
        schema=schema,
        maximum_keyword="maxItems",
        minimum_keyword="minItems",
        unit="items",
    )
    if declared is not None:
        return declared
    return _open_extent_decision(
        identity=identity,
        owner=owner,
        source_pointer=source_pointer,
        dimension="cardinality",
        unit="items",
        extension_owned=extension_owned,
        configuration_document=configuration_document,
    )


def _is_open_map(schema: Mapping[str, Any]) -> bool:
    if schema.get("type") != "object":
        return False
    additional = schema.get("additionalProperties")
    patterns = schema.get("patternProperties")
    return (
        additional is True
        or isinstance(additional, Mapping)
        or (isinstance(patterns, Mapping) and bool(patterns))
    )


def _map_decision(
    *,
    identity: str,
    owner: str,
    source_pointer: str,
    schema: Mapping[str, Any],
    extension_owned: bool,
    configuration_document: bool,
) -> dict[str, object]:
    declared = _declared_cardinality_decision(
        identity=identity,
        owner=owner,
        source_pointer=source_pointer,
        schema=schema,
        maximum_keyword="maxProperties",
        minimum_keyword="minProperties",
        unit="entries",
    )
    if declared is not None:
        return declared
    return _open_extent_decision(
        identity=identity,
        owner=owner,
        source_pointer=source_pointer,
        dimension="cardinality",
        unit="entries",
        extension_owned=extension_owned,
        configuration_document=configuration_document,
    )


def _schema_decisions(
    *,
    section: str,
    authority: str,
    schema: Mapping[str, Any],
    source_pointer: str,
    identity_prefix: str,
    response_arrays: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    include_definitions: bool = True,
) -> list[dict[str, object]]:
    owner, extension_owned = _source_owner(section, authority)
    configuration_document = section == "configuration_documents"
    decisions: list[dict[str, object]] = []
    for pointer, node in _walk_schema(
        schema,
        pointer=source_pointer,
        include_definitions=include_definitions,
    ):
        title = node.get("title")
        field = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        relative_pointer = pointer.removeprefix(source_pointer) or "/"
        identity_base = f"{identity_prefix}:{relative_pointer}"
        read_policy = None
        if response_arrays is not None:
            parts = pointer.split("/")
            if len(parts) >= 3 and parts[-2] == "properties":
                model = parts[-3].replace("~1", "/").replace("~0", "~")
                read_policy = response_arrays.get((model, field))
        if node.get("type") == "array" or "maxItems" in node or "minItems" in node:
            decisions.append(
                _array_decision(
                    identity=f"{identity_base}:cardinality",
                    owner=owner,
                    source_pointer=pointer,
                    schema=node,
                    extension_owned=extension_owned,
                    configuration_document=configuration_document,
                    read_policy=read_policy,
                )
            )
        if _is_open_map(node) or "maxProperties" in node or "minProperties" in node:
            decisions.append(
                _map_decision(
                    identity=f"{identity_base}:map-cardinality",
                    owner=owner,
                    source_pointer=pointer,
                    schema=node,
                    extension_owned=extension_owned,
                    configuration_document=configuration_document,
                )
            )
        encoded_bytes_maximum = node.get("x-riverhog-encoded-bytes-max")
        if isinstance(encoded_bytes_maximum, int) and not isinstance(encoded_bytes_maximum, bool):
            declaration = _extent_declaration(node)
            reason = declaration.get("reason") if declaration is not None else None
            if not isinstance(reason, str) or not reason:
                raise ExtentContractError(
                    f"encoded byte maximum has no {EXTENT_DECLARATION} reason: {pointer}"
                )
            decision = _bound_decision(
                identity=f"{identity_base}:encoded-bytes",
                owner=owner,
                source_pointer=pointer,
                dimension="encoded-size",
                unit="bytes",
                minimum=None,
                maximum=encoded_bytes_maximum,
                reason=reason,
            )
            decision["source_constraint"] = {"field": "x-riverhog-encoded-bytes-max"}
            decisions.append(decision)
        max_length = node.get("maxLength")
        fixed_pattern: str | None = None
        if max_length is None and isinstance(node.get("pattern"), str):
            match = _FIXED_WIDTH_PATTERN.fullmatch(str(node["pattern"]))
            if match is not None:
                fixed_pattern = str(node["pattern"])
                max_length = int(match.group(1))
                node = {**node, "minLength": max_length}
        if isinstance(max_length, int):
            min_length = node.get("minLength")
            decision = _bound_decision(
                identity=f"{identity_base}:length",
                owner=owner,
                source_pointer=pointer,
                dimension="length",
                unit="characters",
                minimum=min_length if isinstance(min_length, int) else None,
                maximum=max_length,
            )
            if fixed_pattern is not None:
                decision["source_constraint"] = {"pattern": fixed_pattern}
            decisions.append(decision)
        maximum = node.get("maximum")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            minimum = node.get("minimum")
            decisions.append(
                _bound_decision(
                    identity=f"{identity_base}:value",
                    owner=owner,
                    source_pointer=pointer,
                    dimension="value",
                    unit="schema-value",
                    minimum=(
                        minimum
                        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
                        else None
                    ),
                    maximum=maximum,
                )
            )
        if (
            node.get("type") in {"integer", "number"}
            and maximum is None
            and _EXTENT_NAME.search(str(title or field))
        ):
            decisions.append(
                _open_extent_decision(
                    identity=f"{identity_base}:open-value",
                    owner=owner,
                    source_pointer=pointer,
                    dimension="value",
                    unit="schema-value",
                    extension_owned=extension_owned,
                    configuration_document=configuration_document,
                )
            )
    return decisions


def _openapi_decisions(openapi_by_application: Mapping[str, Any]) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []
    for application, openapi in sorted(openapi_by_application.items()):
        if not isinstance(openapi, Mapping):
            raise ExtentContractError(f"OpenAPI surface is not a mapping: {application}")
        response_arrays = _direct_response_array_policies(openapi)
        schemas = openapi.get("components", {}).get("schemas", {})
        if not isinstance(schemas, Mapping):
            raise ExtentContractError(f"OpenAPI surface has no component schemas: {application}")
        decisions.extend(
            _schema_decisions(
                section="http_openapi",
                authority=application,
                schema={"schemas": schemas},
                source_pointer=_pointer(
                    "external_contract", "http_openapi", application, "components"
                ),
                identity_prefix=f"http:{application}:components",
                response_arrays=response_arrays,
            )
        )
        for path, path_item in sorted(openapi.get("paths", {}).items()):
            if not isinstance(path_item, Mapping):
                continue
            for method, operation in sorted(path_item.items()):
                if not isinstance(operation, Mapping) or "operationId" not in operation:
                    continue
                operation_id = str(operation["operationId"])
                for parameter_index, parameter in enumerate(operation.get("parameters", [])):
                    if not isinstance(parameter, Mapping):
                        continue
                    parameter_schema = parameter.get("schema")
                    if not isinstance(parameter_schema, Mapping):
                        continue
                    name = str(parameter.get("name") or parameter_index)
                    decisions.extend(
                        _schema_decisions(
                            section="http_openapi",
                            authority=application,
                            schema=parameter_schema,
                            source_pointer=_pointer(
                                "external_contract",
                                "http_openapi",
                                application,
                                "paths",
                                path,
                                method,
                                "parameters",
                                str(parameter_index),
                                "schema",
                            ),
                            identity_prefix=(
                                f"http:{application}:operation:{operation_id}:"
                                f"parameter:{parameter.get('in', 'unknown')}:{name}"
                            ),
                        )
                    )
                request_body = operation.get("requestBody")
                if isinstance(request_body, Mapping):
                    content = request_body.get("content")
                    if isinstance(content, Mapping):
                        for media_type, media in sorted(content.items()):
                            if not isinstance(media, Mapping) or not isinstance(
                                media.get("schema"), Mapping
                            ):
                                continue
                            decisions.extend(
                                _schema_decisions(
                                    section="http_openapi",
                                    authority=application,
                                    schema=media["schema"],
                                    source_pointer=_pointer(
                                        "external_contract",
                                        "http_openapi",
                                        application,
                                        "paths",
                                        path,
                                        method,
                                        "requestBody",
                                        "content",
                                        str(media_type),
                                        "schema",
                                    ),
                                    identity_prefix=(
                                        f"http:{application}:operation:{operation_id}:"
                                        f"request:{media_type}"
                                    ),
                                )
                            )
                responses = operation.get("responses", {})
                if isinstance(responses, Mapping):
                    for status, response in sorted(responses.items()):
                        if not isinstance(response, Mapping):
                            continue
                        content = response.get("content", {})
                        if not isinstance(content, Mapping):
                            continue
                        for media_type, media in sorted(content.items()):
                            if not isinstance(media, Mapping) or not isinstance(
                                media.get("schema"), Mapping
                            ):
                                continue
                            decisions.extend(
                                _schema_decisions(
                                    section="http_openapi",
                                    authority=application,
                                    schema=media["schema"],
                                    source_pointer=_pointer(
                                        "external_contract",
                                        "http_openapi",
                                        application,
                                        "paths",
                                        path,
                                        method,
                                        "responses",
                                        str(status),
                                        "content",
                                        str(media_type),
                                        "schema",
                                    ),
                                    identity_prefix=(
                                        f"http:{application}:operation:{operation_id}:"
                                        f"response:{status}:{media_type}"
                                    ),
                                )
                            )
                read = operation.get("x-riverhog-read-collection")
                if not isinstance(read, Mapping):
                    continue
                decisions.append(
                    {
                        "id": f"http:{application}:operation:{operation_id}:logical-result",
                        "owner": application,
                        "source_pointer": _pointer(
                            "external_contract", "http_openapi", application, "paths", path, method
                        ),
                        "dimension": "logical-result-cardinality",
                        "unit": "items",
                        "policy": "segmented_no_total_max",
                        "rule": "route-progression/v1",
                        "reason": "bounded-route-progression",
                        "progression": dict(read),
                    }
                )
    return decisions


def _cli_decisions(cli_by_application: Mapping[str, Any]) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []

    def visit(application: str, command: Mapping[str, Any], command_path: tuple[str, ...]) -> None:
        command_identity = ":".join(command_path)
        parameters = command.get("parameters", [])
        if not isinstance(parameters, list):
            raise ExtentContractError(
                f"CLI parameters are invalid: {application}:{command_identity}"
            )
        for index, parameter in enumerate(parameters):
            if not isinstance(parameter, Mapping):
                continue
            name = str(parameter.get("name") or index)
            source_pointer = _pointer(
                "external_contract",
                "cli",
                application,
                *(part for command_name in command_path[1:] for part in ("commands", command_name)),
                "parameters",
                str(index),
            )
            identity = f"cli:{application}:{command_identity}:parameter:{name}"
            nargs = parameter.get("nargs")
            if isinstance(nargs, int) and not isinstance(nargs, bool) and nargs >= 0:
                decisions.append(
                    {
                        "id": f"{identity}:values-per-occurrence",
                        "owner": application,
                        "source_pointer": source_pointer,
                        "dimension": "cardinality",
                        "unit": "values-per-occurrence",
                        "policy": "fixed",
                        "rule": "schema-bound/v1",
                        "reason": "fixed-command-argument-arity",
                        "minimum": nargs,
                        "maximum": nargs,
                        "source_constraint": {"field": "nargs"},
                    }
                )
            elif isinstance(nargs, str) and nargs in {"+", "*"}:
                decisions.append(
                    _open_extent_decision(
                        identity=f"{identity}:values-per-occurrence",
                        owner=application,
                        source_pointer=source_pointer,
                        dimension="cardinality",
                        unit="values-per-occurrence",
                        extension_owned=False,
                        configuration_document=False,
                    )
                )
            if parameter.get("multiple") is True or parameter.get("count") is True:
                decisions.append(
                    _open_extent_decision(
                        identity=f"{identity}:occurrences",
                        owner=application,
                        source_pointer=source_pointer,
                        dimension="cardinality",
                        unit="occurrences",
                        extension_owned=False,
                        configuration_document=False,
                    )
                )
            type_ = parameter.get("type")
            if isinstance(type_, Mapping):
                maximum = type_.get("maximum")
                minimum = type_.get("minimum")
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                    decision = _bound_decision(
                        identity=f"{identity}:value",
                        owner=application,
                        source_pointer=source_pointer,
                        dimension="value",
                        unit="cli-value",
                        minimum=(
                            minimum
                            if isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
                            else None
                        ),
                        maximum=maximum,
                    )
                    decision["source_constraint"] = {"field": "type.maximum"}
                    decisions.append(decision)
        commands = command.get("commands", {})
        if not isinstance(commands, Mapping):
            raise ExtentContractError(f"CLI commands are invalid: {application}:{command_identity}")
        for name, child in sorted(commands.items()):
            if isinstance(child, Mapping):
                visit(application, child, (*command_path, str(name)))

    for application, root in sorted(cli_by_application.items()):
        if not isinstance(root, Mapping):
            raise ExtentContractError(f"CLI surface is invalid: {application}")
        visit(application, root, (application,))
    return decisions


def _configuration_environment_decisions(
    environments: list[dict[str, object]],
    patterns: list[dict[str, object]],
) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []
    for index, environment in enumerate(environments):
        name = str(environment.get("name") or "")
        if not _CONFIGURATION_EXTENT_NAME.search(name):
            continue
        consumers = environment.get("consumers", [])
        if not isinstance(consumers, list):
            raise ExtentContractError(f"configuration consumers are invalid: {name}")
        decisions.append(
            {
                "id": f"configuration-environment:{name}:value",
                "owner": ",".join(str(value) for value in consumers),
                "source_pointer": _pointer(
                    "external_contract", "configuration_environment", str(index)
                ),
                "dimension": "value",
                "unit": "configured-value",
                "policy": "operational_policy",
                "rule": "configured-capacity/v1",
                "reason": "operator-configured-capacity",
                "maximum": None,
                "configuration": name,
            }
        )
    for pattern_index, pattern in enumerate(patterns):
        parameters = pattern.get("parameters", {})
        settings = parameters.get("setting", []) if isinstance(parameters, Mapping) else []
        for setting in settings if isinstance(settings, list) else []:
            name = str(setting)
            if not _CONFIGURATION_EXTENT_NAME.search(name):
                continue
            decisions.append(
                {
                    "id": f"configuration-pattern:{pattern.get('template')}:{name}:value",
                    "owner": str(pattern.get("consumer") or ""),
                    "source_pointer": _pointer(
                        "external_contract",
                        "configuration_environment_patterns",
                        str(pattern_index),
                    ),
                    "dimension": "value",
                    "unit": "configured-value",
                    "policy": "operational_policy",
                    "rule": "configured-capacity/v1",
                    "reason": "operator-configured-capacity",
                    "maximum": None,
                    "configuration": name,
                }
            )
    return decisions


def extent_projection(external_contract: Mapping[str, Any]) -> dict[str, object]:
    """Return every schema- and route-owned external extent decision exactly once."""

    decisions = _openapi_decisions(external_contract["http_openapi"])
    decisions.extend(_cli_decisions(external_contract["cli"]))
    decisions.extend(
        _configuration_environment_decisions(
            external_contract["configuration_environment"],
            external_contract["configuration_environment_patterns"],
        )
    )
    for authority, document in sorted(external_contract["configuration_documents"].items()):
        decisions.extend(
            _schema_decisions(
                section="configuration_documents",
                authority=authority,
                schema=document,
                source_pointer=_pointer("external_contract", "configuration_documents", authority),
                identity_prefix=f"configuration:{authority}",
            )
        )
    for authority, document in sorted(external_contract["protocol_schemas"].items()):
        source_pointer = _pointer("external_contract", "protocol_schemas", authority)
        generated = authority.startswith("generated:")
        decisions.extend(
            _schema_decisions(
                section="protocol_schemas",
                authority=authority,
                schema=document,
                source_pointer=source_pointer,
                identity_prefix=f"protocol:{authority}",
                include_definitions=not generated,
            )
        )
        if generated:
            for name, (definition, definition_pointer) in sorted(
                _named_definitions(document, pointer=source_pointer).items()
            ):
                decisions.extend(
                    _schema_decisions(
                        section="protocol_schemas",
                        authority=authority,
                        schema=definition,
                        source_pointer=definition_pointer,
                        identity_prefix=f"protocol:{authority}:definition:{name}",
                        include_definitions=False,
                    )
                )
    ordered = sorted(decisions, key=lambda item: str(item["id"]))
    identities = [str(item["id"]) for item in ordered]
    if len(identities) != len(set(identities)):
        duplicates = sorted(
            identity for identity, count in Counter(identities).items() if count > 1
        )
        raise ExtentContractError(f"extent decisions are not unique: {duplicates}")
    invalid = sorted(
        identity
        for identity, decision in zip(identities, ordered, strict=True)
        if decision.get("policy") not in POLICIES
    )
    if invalid:
        raise ExtentContractError(f"extent decisions have invalid policy: {invalid}")
    invalid_rules = sorted(
        identity
        for identity, decision in zip(identities, ordered, strict=True)
        if decision.get("rule") not in RULES
    )
    if invalid_rules:
        raise ExtentContractError(f"extent decisions have invalid rule: {invalid_rules}")
    unreasoned = sorted(
        identity
        for identity, decision in zip(identities, ordered, strict=True)
        if not isinstance(decision.get("reason"), str) or not decision["reason"]
    )
    if unreasoned:
        raise ExtentContractError(f"extent decisions have no reason: {unreasoned}")
    policy_counts = dict(sorted(Counter(str(item["policy"]) for item in ordered).items()))
    owner_counts = dict(sorted(Counter(str(item["owner"]) for item in ordered).items()))
    content = {
        "schema": SCHEMA,
        "principles": PRINCIPLES,
        "rules": RULES,
        "decisions": ordered,
        "coverage": {
            "discovered": len(ordered),
            "classified": len(ordered),
            "missing": 0,
            "duplicate": 0,
            "stale": 0,
            "undecided": 0,
            "policies": policy_counts,
            "owners": owner_counts,
        },
    }
    return {**content, "sha256": _canonical_sha256(content)}
