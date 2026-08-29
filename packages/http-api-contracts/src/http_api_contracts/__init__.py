from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import ip_address
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

CANONICAL_VISIBLE_TEXT_PATTERN = r"^\S(?:[\s\S]*\S)?$"
CanonicalVisibleText = Annotated[
    str,
    StringConstraints(min_length=1, pattern=CANONICAL_VISIBLE_TEXT_PATTERN),
]
Sha256Identity = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
QuotedSha256Identity = Annotated[
    str,
    StringConstraints(pattern=r'^"[0-9a-f]{64}"$'),
]
_SHA256_IDENTITY = TypeAdapter(Sha256Identity)
_QUOTED_SHA256_IDENTITY = TypeAdapter(QuotedSha256Identity)

FRAMED_BODY_FORMAT = "riverhog-json-opaque-framing/v1"
FRAMED_BODY_MEDIA_TYPE = "application/vnd.riverhog.json-opaque-framing"
FRAMED_BODY_DECLARATION_LENGTH_BYTES = 4
FRAMED_BODY_MAXIMUM_DECLARATION_BYTES = 32 * 1024
JSON_SEQUENCE_MEDIA_TYPE = "application/json-seq"
COMPLETE_ENUMERATION_FORMAT: Literal["riverhog-complete-enumeration/v1"] = (
    "riverhog-complete-enumeration/v1"
)
_JSON_SEQUENCE_RECORD_SEPARATOR = b"\x1e"


class HttpApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(HttpApiModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] | None = None


class ErrorResponse(HttpApiModel):
    error: ErrorBody


class HealthResponse(HttpApiModel):
    service: str = Field(min_length=1)
    status: Literal["ok"]


class CompleteEnumerationItemSchema(HttpApiModel):
    id: CanonicalVisibleText
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CompleteEnumerationBegin(HttpApiModel):
    type: Literal["begin"] = "begin"
    format: Literal["riverhog-complete-enumeration/v1"] = COMPLETE_ENUMERATION_FORMAT
    query: dict[str, Any]
    item_schema: CompleteEnumerationItemSchema


class CompleteEnumerationItem(HttpApiModel):
    type: Literal["item"] = "item"
    ordinal: int = Field(ge=0)
    item: Any


class CompleteEnumerationEnd(HttpApiModel):
    type: Literal["end"] = "end"
    count: int = Field(ge=0)
    items_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def canonical_json_bytes(value: object) -> bytes:
    """Encode one bounded protocol value with the repository canonical JSON rules."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_sha256_identity(value: str) -> str:
    """Validate one exact lowercase SHA-256 HTTP identity."""

    return _SHA256_IDENTITY.validate_python(value, strict=True)


def quote_sha256_identity(value: str) -> str:
    """Return one exact strong quoted SHA-256 HTTP identity."""

    return f'"{validate_sha256_identity(value)}"'


def parse_quoted_sha256_identity(value: str) -> str:
    """Parse one exact strong quoted SHA-256 HTTP identity."""

    return _QUOTED_SHA256_IDENTITY.validate_python(value, strict=True)[1:-1]


def closed_literal_values(literal_type: object) -> frozenset[str]:
    """Return the exact string vocabulary owned by a closed ``Literal`` alias."""

    definition = getattr(literal_type, "__value__", literal_type)
    values = get_args(definition)
    if not values or not all(isinstance(value, str) for value in values):
        raise TypeError("closed literal vocabulary must contain only strings")
    return frozenset(values)


def complete_enumeration_schema_identity(
    item_type: object,
    *,
    schema_id: str,
) -> CompleteEnumerationItemSchema:
    """Seal the exact structural item schema advertised by one stream operation."""

    schema = TypeAdapter(item_type).json_schema(mode="validation")
    return CompleteEnumerationItemSchema(
        id=schema_id,
        sha256=hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
    )


def bounded_list_operation(*, paired_operation_id: str) -> dict[str, Any]:
    """Classify one public read collection as a bounded interactive page."""

    return {
        "x-riverhog-read-collection": {
            "kind": "bounded-list",
            "paired_operation_id": paired_operation_id,
        }
    }


def complete_enumeration_operation(
    *,
    paired_operation_id: str,
    item_type: object,
    schema_id: str,
) -> dict[str, Any]:
    """Classify one public read collection as a complete snapshot stream."""

    item_schema = complete_enumeration_schema_identity(item_type, schema_id=schema_id)
    return {
        "x-riverhog-read-collection": {
            "kind": "complete-enumeration",
            "paired_operation_id": paired_operation_id,
            "format": COMPLETE_ENUMERATION_FORMAT,
            "media_type": JSON_SEQUENCE_MEDIA_TYPE,
            "item_schema": item_schema.model_dump(mode="json"),
        }
    }


def cursor_feed_operation(
    *,
    cursor_parameter: str,
    limit_parameter: str | None,
    fixed_limit: int | None = None,
) -> dict[str, Any]:
    """Classify one public read collection as a bounded cursor/change feed."""

    if (limit_parameter is None) == (fixed_limit is None):
        raise ValueError("a cursor feed requires exactly one variable or fixed limit")
    return {
        "x-riverhog-read-collection": {
            "kind": "cursor-feed",
            "cursor_parameter": cursor_parameter,
            "limit_parameter": limit_parameter,
            "fixed_limit": fixed_limit,
        }
    }


def _json_sequence_record(value: object) -> bytes:
    return _JSON_SEQUENCE_RECORD_SEPARATOR + canonical_json_bytes(value) + b"\n"


def iter_complete_enumeration(
    items: Iterable[object],
    *,
    query: Mapping[str, object],
    item_schema: CompleteEnumerationItemSchema,
) -> Iterator[bytes]:
    """Emit one RFC 7464 complete-enumeration stream without materializing its items."""

    begin = CompleteEnumerationBegin(
        query=dict(query),
        item_schema=item_schema,
    )
    yield _json_sequence_record(begin.model_dump(mode="json"))
    digest = hashlib.sha256()
    count = 0
    for count, value in enumerate(items, start=1):
        item = CompleteEnumerationItem(ordinal=count - 1, item=value)
        encoded = canonical_json_bytes(item.model_dump(mode="json"))
        digest.update(encoded)
        yield _JSON_SEQUENCE_RECORD_SEPARATOR + encoded + b"\n"
    end = CompleteEnumerationEnd(count=count, items_sha256=digest.hexdigest())
    yield _json_sequence_record(end.model_dump(mode="json"))


class CompleteEnumerationReader:
    """Incrementally validate one exact complete-enumeration stream."""

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        item_type: object,
        expected_query: Mapping[str, object],
        expected_item_schema: CompleteEnumerationItemSchema,
    ) -> None:
        self._chunks = iter(chunks)
        self._item_adapter: TypeAdapter[Any] = TypeAdapter(item_type)
        self._expected_query = dict(expected_query)
        self._expected_item_schema = expected_item_schema
        self.complete = False
        self.count = 0
        self.items_sha256: str | None = None
        self._started = False

    def __iter__(self) -> Iterator[Any]:
        if self._started:
            raise RuntimeError("complete-enumeration reader is single-use")
        self._started = True
        records = _iter_json_sequence_records(self._chunks)
        try:
            first = next(records)
        except StopIteration as exc:
            raise ValueError("complete-enumeration stream has no begin frame") from exc
        begin = CompleteEnumerationBegin.model_validate(first)
        if begin.query != self._expected_query:
            raise ValueError("complete-enumeration query identity differs")
        if begin.item_schema != self._expected_item_schema:
            raise ValueError("complete-enumeration item schema identity differs")

        digest = hashlib.sha256()
        ordinal = 0
        for record in records:
            frame_type = record.get("type") if isinstance(record, Mapping) else None
            if frame_type == "end":
                end = CompleteEnumerationEnd.model_validate(record)
                if end.count != ordinal or end.items_sha256 != digest.hexdigest():
                    raise ValueError("complete-enumeration terminal proof differs")
                try:
                    next(records)
                except StopIteration:
                    self.complete = True
                    self.count = end.count
                    self.items_sha256 = end.items_sha256
                    return
                raise ValueError("complete-enumeration stream continues after its terminal frame")
            item = CompleteEnumerationItem.model_validate(record)
            if item.ordinal != ordinal:
                raise ValueError("complete-enumeration item ordinal is not contiguous")
            digest.update(canonical_json_bytes(item.model_dump(mode="json")))
            ordinal += 1
            yield self._item_adapter.validate_python(item.item)
        raise ValueError("complete-enumeration stream has no terminal frame")

    def require_complete(self) -> None:
        if not self.complete:
            raise ValueError("complete-enumeration terminal proof was not consumed")


def _iter_json_sequence_records(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            record = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if not record.startswith(_JSON_SEQUENCE_RECORD_SEPARATOR):
                raise ValueError("JSON sequence record has no record separator")
            parsed = json.loads(record[1:])
            if not isinstance(parsed, dict):
                raise ValueError("JSON sequence frame must be an object")
            yield parsed
    if buffer:
        raise ValueError("JSON sequence ends with an incomplete record")


HttpBodyKind = Literal["none", "json", "framed", "binary"]


@dataclass(frozen=True, slots=True)
class HttpPathParameterContract:
    """One required typed placeholder in an HTTP operation path."""

    name: str
    value_type: object

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.name) is None:
            raise ValueError("HTTP path parameter name is invalid")
        TypeAdapter(self.value_type)


@dataclass(frozen=True, slots=True)
class HttpErrorContract:
    """One exact transport/control error code and its HTTP status."""

    code: str
    status: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.code) is None:
            raise ValueError("HTTP error code must be canonical snake case")
        if self.status < 400 or self.status > 599:
            raise ValueError("HTTP error status must be 4xx or 5xx")


@dataclass(frozen=True, slots=True)
class HttpResponseHeaderContract:
    """One response header described by an executable HTTP operation."""

    name: str
    value_type: object = str
    description: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.name) is None:
            raise ValueError("HTTP response header name is invalid")
        TypeAdapter(self.value_type)


@dataclass(frozen=True, slots=True)
class HttpOperationContract:
    """One exact method/path binding projected into a running OpenAPI document."""

    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"]
    path: str
    request_type: object | None = None
    response_type: object | None = None
    request_kind: HttpBodyKind = "none"
    response_kind: HttpBodyKind = "json"
    success_statuses: tuple[int, ...] = (200,)
    errors: tuple[HttpErrorContract, ...] = ()
    path_parameters: tuple[HttpPathParameterContract, ...] = ()
    response_headers: tuple[HttpResponseHeaderContract, ...] = ()
    error_type: type[BaseModel] = ErrorResponse

    def __post_init__(self) -> None:
        if not self.path.startswith("/v1/"):
            raise ValueError("HTTP operation path must be an absolute v1 path")
        if self.request_kind in {"json", "framed"} and self.request_type is None:
            raise ValueError("typed HTTP request body has no declaration model")
        if self.response_kind in {"json", "framed"} and self.response_type is None:
            raise ValueError("typed HTTP response body has no declaration model")
        if self.response_kind == "none" and self.response_type is not None:
            raise ValueError("empty HTTP response cannot have a response model")
        placeholders = tuple(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", self.path))
        declared = tuple(parameter.name for parameter in self.path_parameters)
        if placeholders != declared or len(declared) != len(set(declared)):
            raise ValueError("HTTP path parameters must exactly match the operation path")
        codes = tuple(error.code for error in self.errors)
        if len(codes) != len(set(codes)):
            raise ValueError("HTTP operation error codes must be unique")
        header_names = tuple(header.name.casefold() for header in self.response_headers)
        if len(header_names) != len(set(header_names)):
            raise ValueError("HTTP response header names must be unique")
        if not isinstance(self.error_type, type) or not issubclass(self.error_type, BaseModel):
            raise TypeError("HTTP error declaration is not a Pydantic model")

    @property
    def error_statuses(self) -> tuple[int, ...]:
        return tuple(sorted({error.status for error in self.errors}))

    def accepts_error(self, *, status: int, code: str) -> bool:
        return HttpErrorContract(code, status) in self.errors

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method:
            return False
        expected = self.path.split("/")
        actual = path.split("/")
        if len(expected) != len(actual):
            return False
        parameters = {parameter.name: parameter for parameter in self.path_parameters}
        for expected_part, actual_part in zip(expected, actual, strict=True):
            if expected_part.startswith("{") and expected_part.endswith("}"):
                if not actual_part:
                    return False
                parameter = parameters[expected_part[1:-1]]
                try:
                    TypeAdapter(parameter.value_type).validate_python(actual_part)
                except ValidationError:
                    return False
            elif expected_part != actual_part:
                return False
        return True


def http_operation_for_request(
    contracts: tuple[HttpOperationContract, ...],
    method: str,
    path: str,
) -> HttpOperationContract | None:
    """Return the one declared operation matching an exact request identity."""

    matches = tuple(contract for contract in contracts if contract.matches(method, path))
    if len(matches) > 1:
        raise ValueError("HTTP operation contracts overlap")
    return matches[0] if matches else None


def http_operation_inventory(
    contracts: Sequence[HttpOperationContract],
) -> list[dict[str, Any]]:
    """Project exact operation contracts into a stable machine-readable inventory."""

    return [
        {
            "method": operation.method,
            "path": operation.path,
            "path_parameters": [
                {
                    "name": parameter.name,
                    "schema": inline_type_schema(parameter.value_type),
                }
                for parameter in operation.path_parameters
            ],
            "request": {
                "kind": operation.request_kind,
                "schema": _type_name(operation.request_type),
            },
            "response": {
                "kind": operation.response_kind,
                "schema": _type_name(operation.response_type),
                "statuses": list(operation.success_statuses),
                "headers": [
                    {
                        "name": header.name,
                        "schema": inline_type_schema(header.value_type),
                        **(
                            {"description": header.description}
                            if header.description is not None
                            else {}
                        ),
                    }
                    for header in operation.response_headers
                ],
            },
            "errors": [{"code": error.code, "status": error.status} for error in operation.errors],
            "error_schema": _type_name(operation.error_type) if operation.errors else None,
        }
        for operation in contracts
    ]


def structural_model_catalog(
    contracts: Sequence[HttpOperationContract],
    *,
    additional_models: Sequence[type[BaseModel]] = (),
) -> dict[str, dict[str, Any]]:
    """Return structural schemas used by operations, without claiming semantic completeness."""

    models: set[type[BaseModel]] = set(additional_models)
    for operation in contracts:
        for value in (operation.request_type, operation.response_type):
            if value is None:
                continue
            if not isinstance(value, type) or not issubclass(value, BaseModel):
                raise TypeError("HTTP declaration is not a Pydantic model")
            models.add(value)
        if operation.errors:
            models.add(operation.error_type)
    return {
        model.__name__: model.model_json_schema(mode="validation")
        for model in sorted(models, key=lambda item: item.__name__)
    }


def _type_name(value: object | None) -> str | None:
    if value is None:
        return None
    name = getattr(value, "__name__", None)
    if not isinstance(name, str) or not name:
        raise TypeError("HTTP declaration type has no stable public name")
    return name


def inline_type_schema(value: object) -> dict[str, Any]:
    """Return a self-contained JSON Schema for an OpenAPI request or response."""

    schema = TypeAdapter(value).json_schema(ref_template="#/$defs/{model}")
    definitions = schema.pop("$defs", {})

    def expand(current: object, trail: frozenset[str] = frozenset()) -> object:
        if isinstance(current, list):
            return [expand(item, trail) for item in current]
        if not isinstance(current, dict):
            return current
        reference = current.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            if name in trail:
                return current
            target = definitions.get(name)
            if isinstance(target, dict):
                return expand(target, trail | {name})
        expanded_items = {str(key): expand(item, trail) for key, item in current.items()}
        discriminator = expanded_items.get("discriminator")
        if isinstance(discriminator, dict) and "propertyName" in discriminator:
            expanded_items["discriminator"] = {"propertyName": discriminator["propertyName"]}
        return expanded_items

    expanded = expand(schema)
    if not isinstance(expanded, dict):  # pragma: no cover - TypeAdapter always returns an object
        raise TypeError("JSON Schema root is not an object")
    return expanded


def operation_openapi(
    contract: HttpOperationContract,
    *,
    error_type: object | None = None,
) -> dict[str, Any]:
    """Build FastAPI route metadata without changing framework-neutral dispatch."""

    responses: dict[int, dict[str, Any]] = {}
    effective_error_type = contract.error_type if error_type is None else error_type
    for status in contract.success_statuses:
        response: dict[str, Any] = {"description": HTTPStatus(status).phrase}
        if contract.response_headers:
            response["headers"] = {
                header.name: {
                    "schema": inline_type_schema(header.value_type),
                    **(
                        {"description": header.description}
                        if header.description is not None
                        else {}
                    ),
                }
                for header in contract.response_headers
            }
        if contract.response_kind == "json":
            response["content"] = {
                "application/json": {"schema": inline_type_schema(contract.response_type)}
            }
        elif contract.response_kind == "framed":
            response["content"] = _framed_body_openapi_content(contract.response_type)
        elif contract.response_kind == "binary":
            response["content"] = {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        responses[status] = response
    for status in contract.error_statuses:
        responses[status] = {
            "description": HTTPStatus(status).phrase,
            "x-riverhog-error-codes": sorted(
                error.code for error in contract.errors if error.status == status
            ),
            "content": {"application/json": {"schema": inline_type_schema(effective_error_type)}},
        }
    extra: dict[str, Any] = {}
    if contract.request_kind == "json":
        extra["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": inline_type_schema(contract.request_type)}},
        }
    elif contract.request_kind == "framed":
        extra["requestBody"] = {
            "required": True,
            "content": _framed_body_openapi_content(contract.request_type),
        }
    if contract.path_parameters:
        extra["parameters"] = [
            {
                "name": parameter.name,
                "in": "path",
                "required": True,
                "schema": inline_type_schema(parameter.value_type),
            }
            for parameter in contract.path_parameters
        ]
    return {
        "status_code": contract.success_statuses[0],
        "response_model": None,
        "responses": responses,
        "openapi_extra": extra or None,
    }


def _framed_body_openapi_content(declaration_type: object) -> dict[str, Any]:
    return {
        FRAMED_BODY_MEDIA_TYPE: {
            "schema": {"type": "string", "format": "binary"},
            "x-riverhog-framing-declaration": inline_type_schema(declaration_type),
            "x-riverhog-framing": {
                "format": FRAMED_BODY_FORMAT,
                "declaration_length_bytes": FRAMED_BODY_DECLARATION_LENGTH_BYTES,
                "declaration_length_byte_order": "big-endian",
                "maximum_declaration_bytes": FRAMED_BODY_MAXIMUM_DECLARATION_BYTES,
                "body": "length || UTF-8 JSON declaration || opaque payload",
            },
        }
    }


ERROR_STATUS_BY_CODE: dict[str, int] = {
    "bad_request": 400,
    "invalid_path": 400,
    "invalid_target": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "length_required": 411,
    "conflict": 409,
    "hash_mismatch": 409,
    "input_upload_storage_hint_invalid": 409,
    "invalid_state": 409,
    "job_template_revision_conflict": 409,
    "storage_hint_mismatch": 409,
    "submission_conflict": 409,
    "download_allowance_exceeded": 429,
    "too_many_active_input_uploads": 429,
    "ingress_failed": 500,
    "internal_error": 500,
    "service_unavailable": 503,
    "insufficient_storage": 507,
}
PUBLIC_ERROR_CODES = frozenset(ERROR_STATUS_BY_CODE)

_ERROR_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    411: "length_required",
    409: "conflict",
    429: "too_many_active_input_uploads",
    500: "internal_error",
    503: "service_unavailable",
    507: "insufficient_storage",
}


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """Declare operation-specific public error codes beside a FastAPI route."""

    unknown = set(codes) - PUBLIC_ERROR_CODES
    if unknown:
        raise ValueError(f"unknown public error codes: {', '.join(sorted(unknown))}")
    grouped: dict[int, list[str]] = {}
    for code in codes:
        grouped.setdefault(ERROR_STATUS_BY_CODE[code], []).append(code)
    return {
        status: {
            "model": ErrorResponse,
            "x-riverhog-error-codes": sorted(set(status_codes)),
        }
        for status, status_codes in grouped.items()
    }


OperationInterface = Literal[
    "human-cli+json",
    "client-only-primitive",
    "standard-tool/protocol",
    "service-internal",
]


def operation_interface(value: OperationInterface) -> dict[str, str]:
    """Declare a non-default operation audience in its generated OpenAPI contract."""

    return {"x-riverhog-interface": value}


def safe_http_base_url(
    value: str,
    *,
    setting: str = "base URL",
    allow_insecure_http: bool = False,
) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"{setting} must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{setting} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{setting} must not contain a query or fragment")
    if parsed.scheme == "https" or allow_insecure_http or _is_loopback_host(parsed.hostname):
        return normalized
    raise ValueError(
        f"{setting} must use HTTPS unless it targets a loopback host "
        "or insecure HTTP is explicitly enabled"
    )


def _is_loopback_host(host: str) -> bool:
    candidate = host.rstrip(".").casefold()
    if candidate == "localhost":
        return True
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


def error_code_for_status(status: int) -> str:
    return _ERROR_CODE_BY_STATUS.get(status, "internal_error" if status >= 500 else "bad_request")


def status_for_error_code(code: str, *, fallback: int = 500) -> int:
    return ERROR_STATUS_BY_CODE.get(code, fallback)


_BASE_OPERATION_ERROR_CODES = frozenset(
    {"bad_request", "unauthorized", "forbidden", "internal_error"}
)


def error_payload(
    *,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=dict(details) if details else None,
        )
    ).model_dump(exclude_none=True)


def apply_openapi_error_contract(
    schema: dict[str, Any],
    *,
    operation_error_codes: Mapping[str, Collection[str]] | None = None,
) -> dict[str, Any]:
    """Project exact application-owned error vocabularies into OpenAPI."""

    declared_by_operation = operation_error_codes or {}
    observed_operations: set[str] = set()
    error_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = error_schema.pop("$defs", {})
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.update(definitions)
    components["ErrorResponse"] = error_schema
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if method not in {"delete", "get", "patch", "post", "put"} or not isinstance(
                operation, dict
            ):
                continue
            if not path.startswith("/v1") and operation.get("x-riverhog-interface") != (
                "standard-tool/protocol"
            ):
                continue
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"OpenAPI operation {method.upper()} {path} has no operationId")
            observed_operations.add(operation_id)
            codes = set(_BASE_OPERATION_ERROR_CODES)
            codes.update(declared_by_operation.get(operation_id, ()))
            for response in responses.values():
                if not isinstance(response, Mapping):
                    continue
                declared = response.get("x-riverhog-error-codes", [])
                if isinstance(declared, list):
                    codes.update(str(code) for code in declared)
            unknown = codes - PUBLIC_ERROR_CODES
            if unknown:
                raise ValueError(
                    "OpenAPI operation declares unknown public error codes: "
                    + ", ".join(sorted(unknown))
                )
            by_status: dict[int, list[str]] = {}
            for code in sorted(codes):
                by_status.setdefault(ERROR_STATUS_BY_CODE[code], []).append(code)
            existing_error_statuses = [
                status for status in responses if str(status).isdigit() and int(str(status)) >= 400
            ]
            for status in existing_error_statuses:
                responses.pop(status, None)
            for status, status_codes in by_status.items():
                status_text = str(status)
                responses[status_text] = {
                    "description": HTTPStatus(status).phrase,
                    "x-riverhog-error-codes": status_codes,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                        }
                    },
                }
    unknown_operations = set(declared_by_operation) - observed_operations
    if unknown_operations:
        raise ValueError(
            "error contracts name unknown OpenAPI operations: "
            + ", ".join(sorted(unknown_operations))
        )
    return schema


def parse_error_payload(
    payload: object,
    *,
    fallback_message: str,
) -> tuple[str, str, dict[str, Any]]:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return "invalid_response", fallback_message, {}
    code = str(error.get("code") or "invalid_response")
    message = str(error.get("message") or fallback_message)
    raw_details = error.get("details")
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    return code, message, details


def parse_declared_error_payload(
    contract: HttpOperationContract,
    *,
    status: int,
    payload: object,
) -> tuple[str, str, dict[str, Any]]:
    """Accept one exact remote rejection or fail as an invalid peer response."""

    response = ErrorResponse.model_validate(payload)
    if not contract.accepts_error(status=status, code=response.error.code):
        raise ValueError("HTTP peer returned an undeclared error code/status pair")
    return response.error.code, response.error.message, dict(response.error.details or {})


__all__ = [
    "CANONICAL_VISIBLE_TEXT_PATTERN",
    "COMPLETE_ENUMERATION_FORMAT",
    "ERROR_STATUS_BY_CODE",
    "FRAMED_BODY_DECLARATION_LENGTH_BYTES",
    "FRAMED_BODY_FORMAT",
    "FRAMED_BODY_MAXIMUM_DECLARATION_BYTES",
    "FRAMED_BODY_MEDIA_TYPE",
    "JSON_SEQUENCE_MEDIA_TYPE",
    "PUBLIC_ERROR_CODES",
    "QuotedSha256Identity",
    "Sha256Identity",
    "CanonicalVisibleText",
    "CompleteEnumerationBegin",
    "CompleteEnumerationEnd",
    "CompleteEnumerationItem",
    "CompleteEnumerationItemSchema",
    "CompleteEnumerationReader",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "HttpErrorContract",
    "HttpBodyKind",
    "HttpOperationContract",
    "HttpPathParameterContract",
    "HttpResponseHeaderContract",
    "OperationInterface",
    "apply_openapi_error_contract",
    "canonical_json_bytes",
    "bounded_list_operation",
    "complete_enumeration_operation",
    "complete_enumeration_schema_identity",
    "cursor_feed_operation",
    "error_code_for_status",
    "error_payload",
    "error_responses",
    "operation_interface",
    "operation_openapi",
    "inline_type_schema",
    "http_operation_for_request",
    "http_operation_inventory",
    "iter_complete_enumeration",
    "parse_error_payload",
    "parse_declared_error_payload",
    "parse_quoted_sha256_identity",
    "quote_sha256_identity",
    "safe_http_base_url",
    "status_for_error_code",
    "structural_model_catalog",
    "validate_sha256_identity",
]
