from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .common import canonical_json
from .constants import OBSERVER_PROFILE


def _schema_directory() -> Traversable:
    return resources.files("riverhog_provenance").joinpath("schemas")


def load_journal_entry_schema() -> dict[str, Any]:
    schema_resource = _schema_directory().joinpath(
        "riverhog-provenance-v1-journal-entry.schema.json"
    )
    return cast(dict[str, Any], json.loads(schema_resource.read_text(encoding="utf-8")))


def load_observer_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for resource in _schema_directory().iterdir():
        if not resource.name.endswith(".schema.json"):
            continue
        document = cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))
        schema_id = document.get("$id")
        if isinstance(schema_id, str):
            schemas[schema_id] = document
    return schemas


def graph_fragment_schema() -> dict[str, Any]:
    root = load_journal_entry_schema()
    return {
        "$schema": root["$schema"],
        "$id": f"{OBSERVER_PROFILE}/graph-fragment.schema.json",
        "$defs": root["$defs"],
        "$ref": "#/$defs/graphFragment",
    }


def _json_nodes(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[tuple[Any, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _json_nodes(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_nodes(child, path + (index,))


def _format_path(path: Iterable[Any]) -> str:
    parts = [str(part) for part in path]
    return "/" + "/".join(parts) if parts else "<root>"


def validate_embedded_typed_values(fragment: Mapping[str, Any]) -> None:
    """Validate every package-defined ``type: json`` value against its schema."""

    schemas = load_observer_schemas()
    findings: list[str] = []
    for path, node in _json_nodes(fragment):
        if not isinstance(node, dict) or node.get("type") != "json":
            continue
        schema_id = node.get("schema")
        schema = schemas.get(schema_id) if isinstance(schema_id, str) else None
        if schema is None:
            continue
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for error in sorted(validator.iter_errors(node.get("data")), key=lambda e: list(e.path)):
            findings.append(
                f"{_format_path(path + ('data',) + tuple(error.absolute_path))}: {error.message}"
            )
    if findings:
        raise ValueError("Observer-defined typed JSON value is invalid:\n" + "\n".join(findings))


def validate_policy_digest(fragment: Mapping[str, Any]) -> None:
    """Verify the capture configuration digest against its semantic assertion."""

    captures = {
        item.get("id"): item
        for item in fragment.get("captures", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    policy_property = f"{OBSERVER_PROFILE}/vocab/observation-policy"
    for extension in fragment.get("extensions", []):
        if not isinstance(extension, dict) or extension.get("property") != policy_property:
            continue
        capture = captures.get(extension.get("subject_id"))
        if not isinstance(capture, dict):
            raise ValueError("observation-policy assertion does not target a local capture")
        value = extension.get("value")
        if not isinstance(value, dict) or value.get("type") != "json":
            raise ValueError("observation-policy assertion must contain a JSON typed value")
        data = value.get("data")
        if not isinstance(data, dict):
            raise ValueError("observation-policy assertion data must be an object")
        actual = hashlib.sha256(canonical_json(data)).hexdigest()
        raw_detail = capture.get("detail")
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        configuration_digest = detail.get("configuration_digest")
        digest = configuration_digest if isinstance(configuration_digest, dict) else {}
        if digest.get("algorithm") != "sha-256" or digest.get("value") != actual:
            raise ValueError("capture configuration digest does not match observation policy")


def validate_graph_fragment(fragment: Mapping[str, Any]) -> None:
    """Validate the structural and observer-profile graph-fragment contract.

    This intentionally does not attempt journal-history reference resolution. The
    Riverhog provenance v1 application validator remains authoritative for a complete journal.
    """

    validator = Draft202012Validator(graph_fragment_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(dict(fragment)), key=lambda e: list(e.path))
    if errors:
        lines = []
        for error in errors:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            lines.append(f"{path}: {error.message}")
        raise ValueError("Riverhog provenance graph fragment is invalid:\n" + "\n".join(lines))
    validate_embedded_typed_values(fragment)
    validate_policy_digest(fragment)


def validate_entry_document(document: Mapping[str, Any]) -> None:
    """Validate one Riverhog provenance v1 journal entry structurally.

    Cross-entry identity, hash-chain, binding-predecessor, and graph-history
    constraints require the full Riverhog provenance application validator and are not
    asserted here.
    """

    validator = Draft202012Validator(load_journal_entry_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(dict(document)), key=lambda e: list(e.path))
    if errors:
        lines = []
        for error in errors:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            lines.append(f"{path}: {error.message}")
        raise ValueError("Riverhog provenance journal entry is invalid:\n" + "\n".join(lines))

    body = document.get("body")
    if isinstance(body, dict):
        assertions = body.get("assertions")
        if isinstance(assertions, dict):
            validate_embedded_typed_values(assertions)
            validate_policy_digest(assertions)
