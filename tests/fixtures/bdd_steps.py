from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
import pytest
from pytest_bdd import given, parsers, then, when

from arc_core.domain.selectors import parse_target
from arc_core.fs_paths import normalize_collection_id
from tests.fixtures.acceptance import AcceptanceSystem
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    IMAGE_ID,
    INVOICE_TARGET,
    PHOTOS_2024_FILE_COUNT,
    PHOTOS_2024_TOTAL_BYTES,
    PHOTOS_COLLECTION_ID,
    PHOTOS_NESTED_COLLECTION_ID,
    PHOTOS_PARENT_COLLECTION_ID,
    SECOND_IMAGE_ID,
    SPLIT_FILE_PARTS,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    TAX_DIRECTORY_TARGET,
    fixture_encrypt_bytes,
)
from tests.fixtures.disc_contracts import (
    InspectedIso,
    assert_collection_manifest_semantics,
    assert_contract_schema,
    assert_disc_manifest_semantics,
    assert_root_layout_contract,
    assert_sidecar_semantics,
    decrypt_yaml_file,
    manifest_entry_by_path,
    payload_bytes,
)

_CAPTURED_WEBHOOK_TIMEOUT_DELAY_SECONDS = 15.0
_DEFAULT_OPTICAL_ACCEPTANCE_DEVICE = "/dev/sr0"


@dataclass(slots=True)
class AcceptanceScenarioContext:
    response: httpx.Response | None = None
    responses: list[httpx.Response] = field(default_factory=list)
    command: Any = None
    command_text: str | None = None
    command_argv: list[str] = field(default_factory=list)
    stdout_json: Any = None
    expected_api_endpoint: tuple[str, str] | None = None
    expected_api_payload: Any = None
    before_collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    after_collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    tracked_collection_id: str | None = None
    inspected_isos: dict[str, InspectedIso] = field(default_factory=dict)
    current_iso: InspectedIso | None = None
    recorded_split_payloads: dict[str, dict[int, bytes]] = field(default_factory=dict)
    recorded_upload_offset: int | None = None
    last_fetch_id: str | None = None
    read_only_browsing_paths: set[str] = field(default_factory=set)
    captured_webhook_payload: dict[str, Any] | None = None
    captured_webhook_attempt: dict[str, Any] | None = None


@pytest.fixture
def acceptance_context() -> AcceptanceScenarioContext:
    return AcceptanceScenarioContext()


def _require_response(context: AcceptanceScenarioContext) -> httpx.Response:
    if context.response is None:  # pragma: no cover - defensive guard
        raise AssertionError("no HTTP response has been recorded for this scenario")
    return context.response


def _require_command(context: AcceptanceScenarioContext) -> Any:
    if context.command is None:  # pragma: no cover - defensive guard
        raise AssertionError("no command has been recorded for this scenario")
    return context.command


def _configured_optical_acceptance_device() -> str:
    return os.environ.get("ARC_DISC_ACCEPTANCE_DEVICE", _DEFAULT_OPTICAL_ACCEPTANCE_DEVICE)


def _run_arc_disc_command(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    *args: str,
) -> None:
    argv = ["arc-disc", *args]
    acceptance_context.command_text = shlex.join(argv)
    acceptance_context.command_argv = argv
    acceptance_context.stdout_json = None
    acceptance_context.expected_api_endpoint = None
    acceptance_context.expected_api_payload = None
    acceptance_context.command = acceptance_system.run_arc_disc(*args)


def _require_inspected_iso(context: AcceptanceScenarioContext) -> InspectedIso:
    if context.current_iso is None:  # pragma: no cover - defensive guard
        raise AssertionError("no ISO has been inspected for this scenario")
    return context.current_iso


def _require_captured_webhook_payload(context: AcceptanceScenarioContext) -> dict[str, Any]:
    if context.captured_webhook_payload is None:  # pragma: no cover - defensive guard
        raise AssertionError("no captured webhook payload has been recorded for this scenario")
    return context.captured_webhook_payload


def _require_captured_webhook_attempt(context: AcceptanceScenarioContext) -> dict[str, Any]:
    if context.captured_webhook_attempt is None:  # pragma: no cover - defensive guard
        raise AssertionError("no captured webhook attempt has been recorded for this scenario")
    return context.captured_webhook_attempt


def _selected_relpath_for_target(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> str:
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    if len(files) != 1:
        raise AssertionError(f"expected exactly one projected file target match for {target!r}")
    return files[0]["path"]


def _selected_content_for_target(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> bytes:
    resp = acceptance_system.request("GET", f"/v1/files/{quote(target, safe='/')}/content")
    if resp.status_code != 200:
        raise AssertionError(
            f"could not get file content for {target!r}: {resp.status_code} {resp.text}"
        )
    return resp.content


def _collection_source_manifest(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> list[dict[str, object]]:
    root = acceptance_system.collection_source_root(collection_id)
    manifest: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return manifest


def _start_collection_upload(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> httpx.Response:
    normalized_collection_id = normalize_collection_id(collection_id)
    return acceptance_system.request(
        "POST",
        "/v1/collection-uploads",
        json_body={
            "collection_id": normalized_collection_id,
            "ingest_source": str(
                acceptance_system.collection_source_root(normalized_collection_id)
            ),
            "files": _collection_source_manifest(acceptance_system, normalized_collection_id),
        },
    )


def _refresh_collection_upload(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> httpx.Response:
    normalized_collection_id = normalize_collection_id(collection_id)
    return acceptance_system.request("GET", f"/v1/collection-uploads/{normalized_collection_id}")


def _upload_collection_file(
    acceptance_system: AcceptanceSystem,
    *,
    collection_id: str,
    path: str,
    offset: int = 0,
    fraction: float = 1.0,
) -> int:
    normalized_collection_id = normalize_collection_id(collection_id)
    session = acceptance_system.request(
        "POST",
        f"/v1/collection-uploads/{normalized_collection_id}/files/{path}/upload",
    )
    assert session.status_code == 200, session.text
    upload = session.json()
    root = acceptance_system.collection_source_root(normalized_collection_id)
    content = (root / path).read_bytes()
    start = int(upload["offset"])
    end = len(content)
    if fraction < 1.0:
        remaining = len(content) - start
        end = start + max(1, int(remaining * fraction))
    chunk = content[start:end]
    response = acceptance_system.request(
        "PATCH",
        str(upload["upload_url"]),
        headers=_tus_chunk_headers(chunk=chunk, offset=start),
        content=chunk,
    )
    assert response.status_code == 204, response.text
    return int(response.headers["Upload-Offset"])


def _tus_chunk_headers(
    *,
    chunk: bytes,
    offset: int,
    content_type: str = "application/offset+octet-stream",
) -> dict[str, str]:
    return {
        "Content-Type": content_type,
        "Tus-Resumable": "1.0.0",
        "Upload-Offset": str(offset),
        "Upload-Checksum": "sha256 "
        + base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii"),
    }


def _json_payload(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _quoted_values(text: str) -> list[str]:
    values: list[str] = []
    remainder = text
    while '"' in remainder:
        _, _, tail = remainder.partition('"')
        value, _, remainder = tail.partition('"')
        values.append(value)
    return values


def _coerce_query_value(value: str) -> object:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return int(value) if value.isdigit() else value


def _query_params(url: str) -> dict[str, object]:
    parts = urlsplit(url)
    return {
        key: _coerce_query_value(value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    }


def _arc_option_value(argv: list[str], option: str, default: object | None = None) -> object | None:
    if option not in argv:
        return default
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise AssertionError(f"missing value for CLI option: {option}")
    return _coerce_query_value(argv[index + 1])


def _arc_bool_flag(argv: list[str], positive: str, negative: str) -> bool | None:
    if positive in argv:
        return True
    if negative in argv:
        return False
    return None


def _set_response(
    context: AcceptanceScenarioContext, response: httpx.Response, *, append: bool = False
) -> None:
    if append:
        context.responses.append(response)
    else:
        context.responses = [response]
    context.response = response


def _response_manifest_entry(
    context: AcceptanceScenarioContext,
    entry_id: str,
) -> dict[str, Any]:
    response = _json_payload(_require_response(context))
    for entry in response["entries"]:
        if entry["id"] == entry_id:
            return entry
    raise AssertionError(f"manifest entry not found: {entry_id}")


def _response_copy_payload(context: AcceptanceScenarioContext) -> dict[str, Any]:
    payload = _json_payload(_require_response(context))
    return payload["copy"]


def _listed_copy_payload(
    context: AcceptanceScenarioContext,
    copy_id: str,
) -> dict[str, Any]:
    payload = _json_payload(_require_response(context))
    for copy in payload["copies"]:
        if copy["id"] == copy_id:
            return copy
    raise AssertionError(f"listed copy not found: {copy_id}")


def _ensure_collection_fixture(acceptance_system: AcceptanceSystem, collection_id: str) -> None:
    if collection_id == DOCS_COLLECTION_ID:
        acceptance_system.seed_docs_hot()
        return
    if collection_id == "tax/files":
        acceptance_system.upload_collection_source(collection_id, DOCS_FILES)
        return
    if collection_id == PHOTOS_COLLECTION_ID:
        acceptance_system.seed_photos_hot()
        return
    if collection_id == PHOTOS_NESTED_COLLECTION_ID:
        acceptance_system.seed_nested_photos_hot()
        return
    if collection_id == PHOTOS_PARENT_COLLECTION_ID:
        acceptance_system.seed_parent_photos_hot()
        return
    raise AssertionError(f"unsupported collection fixture: {collection_id}")


def _ensure_target_fixture(acceptance_system: AcceptanceSystem, target: str) -> None:
    parse_target(target)
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    if resp.status_code == 200 and resp.json()["files"]:
        return

    if target.startswith(f"{DOCS_COLLECTION_ID}/"):
        _ensure_collection_fixture(acceptance_system, DOCS_COLLECTION_ID)
        return
    if target.startswith(f"{PHOTOS_COLLECTION_ID}/"):
        _ensure_collection_fixture(acceptance_system, PHOTOS_COLLECTION_ID)
        return
    if target.startswith(f"{PHOTOS_NESTED_COLLECTION_ID}/") or target == "photos/":
        _ensure_collection_fixture(acceptance_system, PHOTOS_NESTED_COLLECTION_ID)
        return
    if target.startswith(f"{PHOTOS_PARENT_COLLECTION_ID}/"):
        _ensure_collection_fixture(acceptance_system, PHOTOS_PARENT_COLLECTION_ID)
        return

    raise AssertionError(f"unsupported target fixture: {target}")


def _ensure_candidate_fixture(
    acceptance_system: AcceptanceSystem,
    candidate_id: str,
) -> None:
    if candidate_id in {str(current) for current in acceptance_system.state.candidates_by_id}:
        return
    if candidate_id in {fixture.id for fixture in IMAGE_FIXTURES}:
        acceptance_system.seed_planner_fixtures()
        return
    if candidate_id in {fixture.id for fixture in SPLIT_IMAGE_FIXTURES}:
        acceptance_system.seed_split_planner_fixtures()
        return
    raise AssertionError(f"unsupported candidate fixture: {candidate_id}")


def _prepare_arc_expectation(
    acceptance_system: AcceptanceSystem,
    context: AcceptanceScenarioContext,
) -> None:
    argv = context.command_argv
    if not argv or argv[0] != "arc":
        return

    if argv[1] == "pin":
        context.expected_api_endpoint = ("POST", "/v1/pin")
        context.expected_api_payload = acceptance_system.request(
            "POST",
            "/v1/pin",
            json_body={"target": argv[2]},
        ).json()
        return

    if argv[1] == "release":
        context.expected_api_endpoint = ("POST", "/v1/release")
        context.expected_api_payload = acceptance_system.request(
            "POST",
            "/v1/release",
            json_body={"target": argv[2]},
        ).json()
        return

    if argv[1] == "find":
        context.expected_api_endpoint = ("GET", "/v1/search")
        context.expected_api_payload = acceptance_system.request(
            "GET",
            "/v1/search",
            params={"q": argv[2], "limit": 25},
        ).json()
        return

    if argv[1] == "plan":
        context.expected_api_endpoint = ("GET", "/v1/plan")
        params = {
            "page": _arc_option_value(argv, "--page", 1),
            "per_page": _arc_option_value(argv, "--per-page", 25),
            "sort": _arc_option_value(argv, "--sort", "fill"),
            "order": _arc_option_value(argv, "--order", "desc"),
        }
        query = _arc_option_value(argv, "--query")
        collection = _arc_option_value(argv, "--collection")
        iso_ready = _arc_bool_flag(argv, "--iso-ready", "--not-ready")
        if query is not None:
            params["q"] = query
        if collection is not None:
            params["collection"] = collection
        if iso_ready is not None:
            params["iso_ready"] = iso_ready
        context.expected_api_payload = acceptance_system.request(
            "GET", "/v1/plan", params=params
        ).json()
        return

    if argv[1] == "images":
        context.expected_api_endpoint = ("GET", "/v1/images")
        params = {
            "page": _arc_option_value(argv, "--page", 1),
            "per_page": _arc_option_value(argv, "--per-page", 25),
            "sort": _arc_option_value(argv, "--sort", "finalized_at"),
            "order": _arc_option_value(argv, "--order", "desc"),
        }
        query = _arc_option_value(argv, "--query")
        collection = _arc_option_value(argv, "--collection")
        has_copies = _arc_bool_flag(argv, "--has-copies", "--no-copies")
        if query is not None:
            params["q"] = query
        if collection is not None:
            params["collection"] = collection
        if has_copies is not None:
            params["has_copies"] = has_copies
        context.expected_api_payload = acceptance_system.request(
            "GET",
            "/v1/images",
            params=params,
        ).json()
        return

    if argv[1] == "glacier":
        context.expected_api_endpoint = ("GET", "/v1/glacier")
        params: dict[str, object] = {}
        collection = _arc_option_value(argv, "--collection")
        if collection is not None:
            params["collection"] = collection
        context.expected_api_payload = acceptance_system.request(
            "GET",
            "/v1/glacier",
            params=params,
        ).json()
        return

    if argv[1] == "copy" and argv[2] == "add":
        image_id = argv[3]
        context.expected_api_endpoint = ("POST", f"/v1/images/{image_id}/copies")
        body: dict[str, object] = {"location": _arc_option_value(argv, "--at")}
        copy_id = _arc_option_value(argv, "--copy-id")
        if copy_id is not None:
            body["copy_id"] = copy_id
        context.expected_api_payload = acceptance_system.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body=body,
        ).json()
        return

    if argv[1] == "copy" and argv[2] == "list":
        image_id = argv[3]
        context.expected_api_endpoint = ("GET", f"/v1/images/{image_id}/copies")
        context.expected_api_payload = acceptance_system.request(
            "GET",
            f"/v1/images/{image_id}/copies",
        ).json()
        return

    if argv[1] == "copy" and argv[2] == "move":
        image_id = argv[3]
        copy_id = argv[4]
        context.expected_api_endpoint = (
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
        )
        context.expected_api_payload = acceptance_system.request(
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
            json_body={"location": _arc_option_value(argv, "--to")},
        ).json()
        return

    if argv[1] == "copy" and argv[2] == "mark":
        image_id = argv[3]
        copy_id = argv[4]
        body: dict[str, object] = {"state": _arc_option_value(argv, "--state")}
        verification_state = _arc_option_value(argv, "--verification-state")
        location = _arc_option_value(argv, "--at")
        if verification_state is not None:
            body["verification_state"] = verification_state
        if location is not None:
            body["location"] = location
        context.expected_api_endpoint = (
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
        )
        context.expected_api_payload = acceptance_system.request(
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
            json_body=body,
        ).json()
        return

    if argv[1] == "pins":
        context.expected_api_endpoint = ("GET", "/v1/pins")
        context.expected_api_payload = acceptance_system.request("GET", "/v1/pins").json()
        return

    if argv[1] == "fetch":
        return

    if argv[1] == "upload":
        return

    if argv[1] == "show" and "--files" in argv:
        collection_id = argv[2]
        context.expected_api_endpoint = ("GET", f"/v1/collection-files/{collection_id}")
        params = {
            "page": _arc_option_value(argv, "--page", 1),
            "per_page": _arc_option_value(argv, "--per-page", 25),
        }
        context.expected_api_payload = acceptance_system.request(
            "GET",
            f"/v1/collection-files/{quote(collection_id, safe='/')}",
            params=params,
        ).json()
        return

    if argv[1] == "show":
        collection_id = argv[2]
        context.expected_api_endpoint = ("GET", f"/v1/collections/{collection_id}")
        context.expected_api_payload = acceptance_system.request(
            "GET",
            f"/v1/collections/{quote(collection_id, safe='/')}",
        ).json()
        return

    if argv[1] == "status":
        target = argv[2]
        context.expected_api_endpoint = ("GET", "/v1/files")
        params = {
            "target": target,
            "page": _arc_option_value(argv, "--page", 1),
            "per_page": _arc_option_value(argv, "--per-page", 25),
        }
        context.expected_api_payload = acceptance_system.request(
            "GET", "/v1/files", params=params
        ).json()
        return

    raise AssertionError(f"unsupported arc command: {argv}")


@given("an empty archive")
def given_empty_archive() -> None:
    return None


@given(
    parsers.parse('a local collection source "{collection_id}" with deterministic fixture contents')
)
def given_local_collection_source(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    acceptance_system.seed_collection_source(collection_id)


@given(
    parsers.parse('collection "{collection_id}" already exists from deterministic fixture contents')
)
def given_collection_already_uploaded(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    acceptance_system.upload_collection_source(collection_id)


@given(
    parsers.parse('collection upload "{collection_id}" exists for deterministic fixture contents')
)
def given_collection_upload_exists(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    acceptance_system.seed_collection_source(collection_id)
    response = _start_collection_upload(acceptance_system, collection_id)
    assert response.status_code == 200, response.text


@given(parsers.parse('collection upload "{collection_id}" has a partial file upload in progress'))
def given_collection_upload_has_partial_file(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    acceptance_system.seed_collection_source(collection_id)
    response = _start_collection_upload(acceptance_system, collection_id)
    assert response.status_code == 200, response.text
    first_path = str(response.json()["files"][0]["path"])
    acceptance_context.recorded_upload_offset = _upload_collection_file(
        acceptance_system,
        collection_id=collection_id,
        path=first_path,
        fraction=0.5,
    )


@given(parsers.parse('collection upload "{collection_id}" has expired partial upload state'))
def given_collection_upload_has_expired_partial_state(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    acceptance_system.seed_collection_source(collection_id)
    response = _start_collection_upload(acceptance_system, collection_id)
    assert response.status_code == 200, response.text
    first_path = str(response.json()["files"][0]["path"])
    _upload_collection_file(
        acceptance_system,
        collection_id=collection_id,
        path=first_path,
        fraction=0.5,
    )
    acceptance_system.expire_collection_upload(collection_id)


@given(parsers.parse('fetch "{fetch_id}" has expired partial upload state for entry "{entry_id}"'))
def given_fetch_has_expired_partial_upload_state(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
    entry_id: str,
) -> None:
    given_fetch_has_partial_upload_in_progress(
        acceptance_system,
        acceptance_context,
        fetch_id,
        entry_id,
    )
    acceptance_system.expire_fetch_upload(fetch_id, entry_id)


@given(parsers.parse('an archive containing collection "{collection_id}"'))
def given_archive_containing_collection(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    _ensure_collection_fixture(acceptance_system, collection_id)


@given("an archive containing deterministic fixture collections")
def given_archive_with_search_fixtures(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_search_fixtures()


@given("an archive with planner fixtures")
@given("an archive with planned images")
def given_archive_with_planner_fixtures(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()


@given("an archive with split planner fixtures")
@given("an archive with split planned images")
def given_archive_with_split_planner_fixtures(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_split_planner_fixtures()


@given("the spec harness exposes controlled Glacier billing metadata")
def given_spec_harness_exposes_controlled_glacier_billing_metadata(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.state.glacier_billing_metadata_available = True


@given("the planner has at least one candidate image")
def given_planner_has_candidate_image(acceptance_system: AcceptanceSystem) -> None:
    plan = acceptance_system.planning.get_plan()
    candidates = plan["candidates"]
    assert isinstance(candidates, list)
    assert candidates


@given(parsers.parse('collection "{collection_id}" exists and is fully hot'))
def given_collection_exists_and_is_fully_hot(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    _ensure_collection_fixture(acceptance_system, collection_id)


@given(parsers.parse('collection "{collection_id}" has uploaded Glacier archive package'))
def given_collection_has_uploaded_glacier_archive_package(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    _ensure_collection_fixture(acceptance_system, collection_id)
    acceptance_system.mark_collection_archive_uploaded(collection_id)


@given(
    parsers.parse(
        'collection Glacier archiving fails for "{collection_id}" with error "{error}"'
    )
)
@given(
    parsers.parse(
        'the glacier upload fixture fails for collection "{collection_id}" with error "{error}"'
    )
)
def given_collection_glacier_archiving_fails(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    error: str,
) -> None:
    acceptance_system.fail_collection_glacier_upload(collection_id, error=error)


@given(
    parsers.parse(
        'collection upload "{collection_id}" has completed file verification and is archiving'
    )
)
def given_collection_upload_has_completed_verification_and_is_archiving(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = acceptance_system.stage_collection_upload_archiving(collection_id)
    _set_response(acceptance_context, httpx.Response(200, json=payload))
    assert payload["state"] == "archiving"
    acceptance_system.seed_candidate_for_collection(collection_id)


@given(parsers.parse('target "{target}" is already pinned'))
@given(parsers.parse('target "{target}" is pinned'))
def given_target_is_pinned(acceptance_system: AcceptanceSystem, target: str) -> None:
    _ensure_target_fixture(acceptance_system, target)
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('target "{target}" is not pinned'))
def given_target_is_not_pinned(acceptance_system: AcceptanceSystem, target: str) -> None:
    resp = acceptance_system.request("POST", "/v1/release", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('target "{target}" is valid'))
def given_target_is_valid(acceptance_system: AcceptanceSystem, target: str) -> None:
    parse_target(target)
    _ensure_target_fixture(acceptance_system, target)


@given(parsers.parse('file "{target}" is archived'))
def given_file_is_archived(acceptance_system: AcceptanceSystem, target: str) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert files
    assert all(record["archived"] for record in files)


@given(parsers.parse('file "{target}" is not hot'))
def given_file_is_not_hot(acceptance_system: AcceptanceSystem, target: str) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert not (bool(files) and all(record["hot"] for record in files))


@given(parsers.parse('hot backing bytes for file "{target}" are missing'))
def given_hot_backing_bytes_for_file_are_missing(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    acceptance_system.seed_docs_hot()
    acceptance_system.delete_hot_backing_file(target)


@given(parsers.parse('archived target "{target}" is pinned with fetch "{fetch_id}"'))
def given_archived_target_is_pinned_with_fetch(
    acceptance_system: AcceptanceSystem,
    target: str,
    fetch_id: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('split archived fetch "{fetch_id}" exists for target "{target}"'))
def given_split_archived_fetch_exists(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
    target: str,
) -> None:
    acceptance_system.seed_docs_archive_with_split_invoice()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('split archived target "{target}" is pinned with fetch "{fetch_id}"'))
def given_split_archived_target_is_pinned_with_fetch(
    acceptance_system: AcceptanceSystem,
    target: str,
    fetch_id: str,
) -> None:
    acceptance_system.seed_docs_archive_with_split_invoice()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('fetch "{fetch_id}" already exists for target "{target}"'))
@given(parsers.parse('fetch "{fetch_id}" exists for target "{target}"'))
def given_fetch_exists_for_target(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
    target: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('fetch "{fetch_id}" exists'))
def given_fetch_exists(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": TAX_DIRECTORY_TARGET})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('fetch "{fetch_id}" has a stable manifest'))
def given_fetch_has_stable_manifest(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    first = acceptance_system.fetches.manifest(fetch_id)
    second = acceptance_system.fetches.manifest(fetch_id)
    assert first == second


@given(parsers.parse('fetch "{fetch_id}" has entry "{entry_id}" with a partial upload in progress'))
def given_fetch_has_partial_upload_in_progress(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
    entry_id: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})
    assert resp.status_code == 200, resp.text
    manifest = acceptance_system.fetches.manifest(fetch_id)
    entry_ids = [item["id"] for item in manifest["entries"]]
    assert entry_id in entry_ids
    acceptance_context.recorded_upload_offset = acceptance_system.upload_partial_entry(
        fetch_id, entry_id
    )


@given("a configured optical reader can recover every required entry")
@when("a configured optical reader can recover every required entry")
def given_arc_disc_success_fixture(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1")


@given("the optical reader fixture fails for one required entry")
@given("the configured optical reader cannot recover one required entry")
def given_arc_disc_reader_failure_fixture(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.configure_arc_disc_fixture(
        fetch_id="fx-1",
        fail_path=SPLIT_FILE_RELPATH,
    )


@given("the optical reader fixture returns incorrect recovered bytes for one required entry")
@given("the configured optical reader returns bytes the server rejects for one required entry")
def given_arc_disc_server_validation_failure_fixture(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.configure_arc_disc_fixture(
        fetch_id="fx-1",
        corrupt_path=SPLIT_FILE_RELPATH,
    )


@given(parsers.parse('the optical reader fixture fails for copy id "{copy_id}"'))
@when(parsers.parse('the optical reader fixture fails for copy id "{copy_id}"'))
@given(parsers.parse('the configured optical reader cannot recover copy id "{copy_id}"'))
@when(parsers.parse('the configured optical reader cannot recover copy id "{copy_id}"'))
def given_arc_disc_reader_failure_for_copy(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1", fail_copy_ids={copy_id})


@given(
    parsers.parse('the burn fixture confirms labeled copy id "{copy_id}" at location "{location}"')
)
@when(
    parsers.parse('the burn fixture confirms labeled copy id "{copy_id}" at location "{location}"')
)
@then(
    parsers.parse('the burn fixture confirms labeled copy id "{copy_id}" at location "{location}"')
)
@given(parsers.parse('the operator confirms labeled copy id "{copy_id}" at location "{location}"'))
@when(parsers.parse('the operator confirms labeled copy id "{copy_id}" at location "{location}"'))
@then(parsers.parse('the operator confirms labeled copy id "{copy_id}" at location "{location}"'))
def given_burn_fixture_confirms_labeled_copy(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
    location: str,
) -> None:
    acceptance_system.confirm_arc_disc_burn_copy(copy_id, location=location)


@given(parsers.parse('the burn fixture says unlabeled copy id "{copy_id}" is still available'))
@when(parsers.parse('the burn fixture says unlabeled copy id "{copy_id}" is still available'))
@given(parsers.parse('unlabeled copy id "{copy_id}" is still available'))
@when(parsers.parse('unlabeled copy id "{copy_id}" is still available'))
def given_burn_fixture_says_unlabeled_copy_is_available(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    acceptance_system.set_arc_disc_burn_copy_available(copy_id, available=True)


@given(parsers.parse('the burn fixture says unlabeled copy id "{copy_id}" is unavailable'))
@when(parsers.parse('the burn fixture says unlabeled copy id "{copy_id}" is unavailable'))
@given(parsers.parse('unlabeled copy id "{copy_id}" is unavailable'))
@when(parsers.parse('unlabeled copy id "{copy_id}" is unavailable'))
def given_burn_fixture_says_unlabeled_copy_is_unavailable(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    acceptance_system.set_arc_disc_burn_copy_available(copy_id, available=False)


@given(parsers.parse('the burn fixture fails while burning copy id "{copy_id}"'))
@when(parsers.parse('the burn fixture fails while burning copy id "{copy_id}"'))
@then(parsers.parse('the burn fixture fails while burning copy id "{copy_id}"'))
@given(parsers.parse('burning copy id "{copy_id}" fails'))
@when(parsers.parse('burning copy id "{copy_id}" fails'))
@then(parsers.parse('burning copy id "{copy_id}" fails'))
def given_burn_fixture_fails_while_burning_copy(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    acceptance_system.fail_arc_disc_burn_copy(copy_id)


@given(parsers.parse('the burn fixture fails while verifying burned media for copy id "{copy_id}"'))
@when(parsers.parse('the burn fixture fails while verifying burned media for copy id "{copy_id}"'))
@then(parsers.parse('the burn fixture fails while verifying burned media for copy id "{copy_id}"'))
@given(parsers.parse('burned-media verification fails for copy id "{copy_id}"'))
@when(parsers.parse('burned-media verification fails for copy id "{copy_id}"'))
@then(parsers.parse('burned-media verification fails for copy id "{copy_id}"'))
def given_burn_fixture_fails_while_verifying_burned_media(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    acceptance_system.fail_arc_disc_burn_copy_verification(copy_id)


@when("the burn fixture clears all burn failures")
@when("the optical burn boundary is healthy again")
def when_burn_fixture_clears_failures(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.clear_arc_disc_burn_failures()


@when(parsers.parse('the staged ISO for image "{image_id}" is corrupted'))
def when_staged_iso_is_corrupted(
    acceptance_system: AcceptanceSystem,
    image_id: str,
) -> None:
    acceptance_system.corrupt_arc_disc_staged_iso(image_id)


@then(parsers.parse('the staged ISO for image "{image_id}" is absent'))
def then_staged_iso_is_absent(
    acceptance_system: AcceptanceSystem,
    image_id: str,
) -> None:
    assert not acceptance_system.arc_disc_staged_iso_exists(image_id)


@given(parsers.parse('fetch "{fetch_id}" exists with entry "{entry_id}"'))
def given_fetch_exists_with_entry(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
    entry_id: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})
    assert resp.status_code == 200, resp.text
    manifest = acceptance_system.fetches.manifest(fetch_id)
    entry_ids = [item["id"] for item in manifest["entries"]]
    assert entry_id in entry_ids


@given(parsers.parse('entry "{entry_id}" expects sha256 "{sha256}"'))
def given_entry_expected_hash(
    acceptance_system: AcceptanceSystem,
    entry_id: str,
    sha256: str,
) -> None:
    manifest = acceptance_system.request("GET", "/v1/fetches/fx-1/manifest").json()
    entry = next((e for e in manifest["entries"] if e["id"] == entry_id), None)
    assert entry is not None, f"entry {entry_id!r} not found in manifest"
    if sha256 == "good-hash":
        assert entry["sha256"]
        return
    assert entry["sha256"] == sha256


@given(parsers.parse('fetch "{fetch_id}" is not done'))
@given(parsers.parse('fetch "{fetch_id}" is not failed'))
def given_fetch_is_not_terminal(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    state = acceptance_system.fetches.get(fetch_id).state.value
    assert state not in {"done", "failed"}


@given(
    parsers.parse(
        'every required fetch entry for "{fetch_id}" has been uploaded with the correct bytes'
    )
)
def given_fetch_entries_are_uploaded(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    target = str(acceptance_system.fetches.get(fetch_id).target)
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text
    acceptance_system.upload_required_entries(fetch_id)


@given(parsers.parse('pinning target "{target}" requires fetch "{fetch_id}"'))
def given_pinning_target_requires_fetch(
    acceptance_system: AcceptanceSystem,
    target: str,
    fetch_id: str,
) -> None:
    acceptance_system.seed_docs_archive()
    resp = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert resp.status_code == 200, resp.text


@given(parsers.parse('candidate "{candidate_id}" exists'))
def given_candidate_exists(acceptance_system: AcceptanceSystem, candidate_id: str) -> None:
    resp = acceptance_system.request("GET", "/v1/plan", params={"q": candidate_id})
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    assert any(c["candidate_id"] == candidate_id for c in candidates)


@given(parsers.parse('candidate "{candidate_id}" has iso_ready true'))
def given_candidate_has_iso_ready_true(
    acceptance_system: AcceptanceSystem, candidate_id: str
) -> None:
    resp = acceptance_system.request("GET", "/v1/plan", params={"q": candidate_id})
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
    assert candidate is not None
    assert candidate["iso_ready"] is True


@given(parsers.parse('candidate "{candidate_id}" covers bytes from collection "{collection_id}"'))
def given_candidate_covers_collection(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    candidate_id: str,
    collection_id: str,
) -> None:
    resp = acceptance_system.request("GET", "/v1/plan", params={"q": candidate_id})
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
    assert candidate is not None
    assert collection_id in candidate["collection_ids"]
    acceptance_context.tracked_collection_id = collection_id


@given(parsers.parse('copy "{copy_id}" already exists'))
def given_copy_already_exists(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
) -> None:
    _ensure_candidate_fixture(acceptance_system, IMAGE_ID)
    acceptance_system.planning.finalize_image(IMAGE_ID)
    acceptance_system.copies.register("20260420T040001Z", "Shelf B1", copy_id=copy_id)


@given(parsers.parse('candidate "{candidate_id}" is finalized'))
@when(parsers.parse('candidate "{candidate_id}" is finalized'))
def given_candidate_is_finalized(
    acceptance_system: AcceptanceSystem,
    candidate_id: str,
) -> None:
    _ensure_candidate_fixture(acceptance_system, candidate_id)
    acceptance_system.planning.finalize_image(candidate_id)


@given('fixture finalized image "20260420T040002Z" exists for collection "photos-2024"')
def given_fixture_finalized_image_exists_for_photos(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_finalized_image(SECOND_IMAGE_ID, force_ready=True)


@given(parsers.parse('image rebuild session "{session_id}" exists for image "{image_id}"'))
def given_image_rebuild_session_exists_for_image(
    acceptance_system: AcceptanceSystem,
    session_id: str,
    image_id: str,
) -> None:
    acceptance_system.ensure_image_rebuild_session(session_id=session_id, image_id=image_id)


@given(parsers.parse('collection "{collection_id}" contains file "{path}"'))
def given_collection_contains_file(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    path: str,
) -> None:
    request_path = f"/v1/collection-files/{quote(collection_id, safe='/')}"
    resp = acceptance_system.request("GET", request_path)
    assert resp.status_code == 200, resp.text
    collection_files = {record["path"] for record in resp.json()["files"]}
    assert path.lstrip("/") in collection_files


@given(parsers.parse('collection "{collection_id}" keeps only path "{path}" and is archived'))
def given_collection_keeps_only_path_and_is_archived(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    path: str,
) -> None:
    acceptance_system.constrain_collection_to_paths(
        collection_id,
        [path],
        hot=False,
        archived=True,
    )


@given(
    parsers.parse(
        'collection "{collection_id}" keeps only finalized image '
        '"{image_id}" coverage and is archived'
    )
)
def given_collection_keeps_only_finalized_image_coverage_and_is_archived(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    image_id: str,
) -> None:
    acceptance_system.constrain_collection_to_finalized_image_coverage(
        collection_id,
        image_id,
        hot=False,
        archived=True,
    )


@given(parsers.parse('collection "{collection_id}" contains directory "{path}"'))
def given_collection_contains_directory(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    path: str,
) -> None:
    prefix = path.strip("/").rstrip("/") + "/"
    request_path = f"/v1/collection-files/{quote(collection_id, safe='/')}"
    resp = acceptance_system.request("GET", request_path)
    assert resp.status_code == 200, resp.text
    collection_files = [record["path"] for record in resp.json()["files"]]
    assert any(current.startswith(prefix) for current in collection_files)


@when(parsers.parse('the client gets "{url}"'))
@then(parsers.parse('the client gets "{url}"'))
def when_client_gets_url(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    url: str,
) -> None:
    parts = urlsplit(url)
    response = acceptance_system.request("GET", parts.path, params=_query_params(url))
    _set_response(acceptance_context, response)


@when(parsers.parse('the client sends HEAD to "{url}"'))
def when_client_heads_url(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    url: str,
) -> None:
    parts = urlsplit(url)
    response = acceptance_system.request("HEAD", parts.path, params=_query_params(url))
    _set_response(acceptance_context, response)


@when(parsers.parse('the client sends DELETE to "{url}"'))
def when_client_deletes_url(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    url: str,
) -> None:
    parts = urlsplit(url)
    response = acceptance_system.request("DELETE", parts.path, params=_query_params(url))
    _set_response(acceptance_context, response)


@when(parsers.parse('the client gets "{url}" again'))
def when_client_gets_url_again(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    url: str,
) -> None:
    parts = urlsplit(url)
    response = acceptance_system.request("GET", parts.path, params=_query_params(url))
    _set_response(acceptance_context, response, append=True)


@when(parsers.parse('the client downloads and inspects ISO for image "{image_id}"'))
def when_client_downloads_and_inspects_iso(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    response = acceptance_system.request("GET", f"/v1/images/{image_id}/iso")
    _set_response(acceptance_context, response)
    inspected = acceptance_system.inspect_downloaded_iso(
        image_id=image_id,
        iso_bytes=response.content,
    )
    acceptance_context.inspected_isos[image_id] = inspected
    acceptance_context.current_iso = inspected


@given(parsers.parse('the client posts to "{path}"'))
@when(parsers.parse('the client posts to "{path}"'))
def when_client_posts(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
) -> None:
    response = acceptance_system.request("POST", path)
    _set_response(acceptance_context, response)


@when(
    parsers.parse(
        'the client posts to "{path}" to materialize collection file "{file_path}"'
    )
)
def when_client_posts_to_materialize_collection_file(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    file_path: str,
) -> None:
    response = acceptance_system.request(
        "POST",
        path,
        json_body={"paths": [file_path]},
    )
    _set_response(acceptance_context, response)


@when(parsers.parse('the client posts to "{path}" again'))
def when_client_posts_again(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
) -> None:
    response = acceptance_system.request("POST", path)
    _set_response(acceptance_context, response, append=True)


@when(
    parsers.parse(
        'the client sends PATCH to "{path}" with upload chunk content type "{content_type}"'
    )
)
def when_client_patches_upload_path_with_chunk_content_type(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    content_type: str,
) -> None:
    chunk = b"chunk-bytes"
    response = acceptance_system.request(
        "PATCH",
        path,
        headers=_tus_chunk_headers(chunk=chunk, offset=0, content_type=content_type),
        content=chunk,
    )
    _set_response(acceptance_context, response)


@when(parsers.parse('the client creates or resumes collection upload "{collection_id}"'))
def when_client_creates_or_resumes_collection_upload(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    response = _start_collection_upload(acceptance_system, collection_id)
    _set_response(acceptance_context, response)


@when(parsers.parse('background expiry cleanup removes collection upload "{collection_id}"'))
def when_background_cleanup_removes_collection_upload(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    acceptance_system.wait_for_collection_upload_cleanup(collection_id)


@when(parsers.parse('background expiry cleanup resets fetch "{fetch_id}" entry "{entry_id}"'))
def when_background_cleanup_resets_fetch_entry(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
    entry_id: str,
) -> None:
    acceptance_system.wait_for_fetch_upload_cleanup(fetch_id, entry_id)


@when(parsers.parse('the client creates or resumes collection upload "{collection_id}" again'))
def when_client_creates_or_resumes_collection_upload_again(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    response = _start_collection_upload(acceptance_system, collection_id)
    _set_response(acceptance_context, response, append=True)


@when(parsers.parse('the client uploads every required file for collection "{collection_id}"'))
def when_client_uploads_every_required_collection_file(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    response = _start_collection_upload(acceptance_system, collection_id)
    assert response.status_code == 200, response.text
    for file_payload in response.json()["files"]:
        _upload_collection_file(
            acceptance_system,
            collection_id=collection_id,
            path=str(file_payload["path"]),
        )
    normalized_collection_id = normalize_collection_id(collection_id)
    failure_configured = acceptance_system.collection_glacier_failure_configured(
        normalized_collection_id
    )
    if failure_configured:
        payload = acceptance_system.wait_for_collection_upload_state(collection_id, "failed")
    elif "/" in normalized_collection_id:
        payload = acceptance_system.wait_for_collection_upload_state(collection_id, "finalized")
    else:
        refresh = _refresh_collection_upload(acceptance_system, collection_id)
        assert refresh.status_code == 200, refresh.text
        payload = refresh.json()
    if payload.get("state") == "archiving":
        acceptance_system.defer_collection_glacier_archiving(collection_id)
    _set_response(acceptance_context, httpx.Response(200, json=payload))


@given(parsers.parse('the client waits for collection upload "{collection_id}" state "{state}"'))
@when(parsers.parse('the client waits for collection upload "{collection_id}" state "{state}"'))
def when_client_waits_for_collection_upload_state(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = acceptance_system.wait_for_collection_upload_state(collection_id, state)
    _set_response(acceptance_context, httpx.Response(200, json=payload))


@when(parsers.parse('the client retries collection Glacier archiving for "{collection_id}"'))
def when_client_retries_collection_glacier_archiving(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    acceptance_system.clear_collection_glacier_upload_failure(collection_id)
    response = _start_collection_upload(acceptance_system, collection_id)
    assert response.status_code == 200, response.text
    payload = acceptance_system.wait_for_collection_upload_state(collection_id, "finalized")
    _set_response(acceptance_context, httpx.Response(200, json=payload))


@when(parsers.parse('collection "{collection_id}" starts Glacier archiving'))
def when_collection_starts_glacier_archiving(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    acceptance_system.start_collection_glacier_archiving(collection_id)
    payload = acceptance_system.wait_for_collection_upload_state(collection_id, "failed")
    _set_response(acceptance_context, httpx.Response(200, json=payload))


@when(parsers.parse('the client refreshes collection upload "{collection_id}"'))
def when_client_refreshes_collection_upload(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    response = _refresh_collection_upload(acceptance_system, collection_id)
    _set_response(acceptance_context, response)


@given(parsers.parse('the client posts to "{path}" with target "{target}"'))
@when(parsers.parse('the client posts to "{path}" with target "{target}"'))
def when_client_posts_with_target(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    target: str,
) -> None:
    response = acceptance_system.request("POST", path, json_body={"target": target})
    _set_response(acceptance_context, response)


@given(parsers.parse('the client posts to "{path}" with id "{copy_id}" and location "{location}"'))
@when(parsers.parse('the client posts to "{path}" with id "{copy_id}" and location "{location}"'))
def when_client_registers_copy(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    copy_id: str,
    location: str,
) -> None:
    if acceptance_context.tracked_collection_id is not None:
        before = acceptance_system.request(
            "GET",
            f"/v1/collections/{acceptance_context.tracked_collection_id}",
        ).json()
        acceptance_context.before_collections[acceptance_context.tracked_collection_id] = before
    response = acceptance_system.request(
        "POST",
        path,
        json_body={"copy_id": copy_id, "location": location},
    )
    _set_response(acceptance_context, response)
    if acceptance_context.tracked_collection_id is not None:
        after = acceptance_system.request(
            "GET",
            f"/v1/collections/{acceptance_context.tracked_collection_id}",
        ).json()
        acceptance_context.after_collections[acceptance_context.tracked_collection_id] = after


@given(parsers.parse('the client posts to "{path}" with location "{location}"'))
@when(parsers.parse('the client posts to "{path}" with location "{location}"'))
def when_client_registers_generated_copy(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    location: str,
) -> None:
    if acceptance_context.tracked_collection_id is not None:
        before = acceptance_system.request(
            "GET",
            f"/v1/collections/{acceptance_context.tracked_collection_id}",
        ).json()
        acceptance_context.before_collections[acceptance_context.tracked_collection_id] = before
    response = acceptance_system.request(
        "POST",
        path,
        json_body={"location": location},
    )
    _set_response(acceptance_context, response)
    if acceptance_context.tracked_collection_id is not None:
        after = acceptance_system.request(
            "GET",
            f"/v1/collections/{acceptance_context.tracked_collection_id}",
        ).json()
        acceptance_context.after_collections[acceptance_context.tracked_collection_id] = after


@when(
    parsers.parse(
        'the client patches "{path}" with location "{location}", state "{state}", '
        'and verification_state "{verification_state}"'
    )
)
def when_client_patches_copy(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    location: str,
    state: str,
    verification_state: str,
) -> None:
    response = acceptance_system.request(
        "PATCH",
        path,
        json_body={
            "location": location,
            "state": state,
            "verification_state": verification_state,
        },
    )
    _set_response(acceptance_context, response)


@given(parsers.parse('the client patches "{path}" with state "{state}"'))
@when(parsers.parse('the client patches "{path}" with state "{state}"'))
def when_client_patches_copy_state_only(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    state: str,
) -> None:
    response = acceptance_system.request("PATCH", path, json_body={"state": state})
    _set_response(acceptance_context, response)


@given(
    parsers.parse(
        'the client patches "{path}" with state "{state}" '
        'and verification_state "{verification_state}"'
    )
)
@when(
    parsers.parse(
        'the client patches "{path}" with state "{state}" '
        'and verification_state "{verification_state}"'
    )
)
def when_client_patches_copy_state_and_verification(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
    state: str,
    verification_state: str,
) -> None:
    response = acceptance_system.request(
        "PATCH",
        path,
        json_body={
            "state": state,
            "verification_state": verification_state,
        },
    )
    _set_response(acceptance_context, response)


@when("the API process restarts")
def when_api_process_restarts(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.restart()


@given(parsers.parse('the captured webhook sink fails event "{event}" with status {status:d} once'))
@given(
    parsers.parse(
        'the captured webhook sink fails event "{event}" '
        'with status {status:d} for {count:d} attempts'
    )
)
def given_captured_webhook_sink_fails_event(
    acceptance_system: AcceptanceSystem,
    event: str,
    status: int,
    count: int = 1,
) -> None:
    acceptance_system.configure_webhook_failure(
        event,
        status_code=status,
        remaining=count,
    )


@given(parsers.parse('the captured webhook sink times out event "{event}" once'))
@given(
    parsers.parse(
        'the captured webhook sink times out event "{event}" for {count:d} attempts'
    )
)
def given_captured_webhook_sink_times_out_event(
    acceptance_system: AcceptanceSystem,
    event: str,
    count: int = 1,
) -> None:
    acceptance_system.configure_webhook_failure(
        event,
        remaining=count,
        delay_seconds=_CAPTURED_WEBHOOK_TIMEOUT_DELAY_SECONDS,
        mode="timeout",
    )


@given(parsers.parse('the client waits for collection "{collection_id}" glacier state "{state}"'))
@when(parsers.parse('the client waits for collection "{collection_id}" glacier state "{state}"'))
def when_the_client_waits_for_collection_glacier_state(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    state: str,
) -> None:
    acceptance_system.wait_for_collection_glacier_state(collection_id, state)


@given(parsers.parse('the client waits for recovery session "{session_id}" state "{state}"'))
@when(parsers.parse('the client waits for recovery session "{session_id}" state "{state}"'))
def when_the_client_waits_for_recovery_session_state(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    session_id: str,
    state: str,
) -> None:
    acceptance_system.wait_for_recovery_session_state(session_id, state)
    response = acceptance_system.request("GET", f"/v1/recovery-sessions/{session_id}")
    _set_response(acceptance_context, response)


@given(parsers.parse('the client waits for captured webhook event "{event}"'))
@when(parsers.parse('the client waits for captured webhook event "{event}"'))
@then(parsers.parse('the client waits for captured webhook event "{event}"'))
@given(
    parsers.parse(
        'the client waits up to {timeout:d} seconds for captured webhook event "{event}"'
    )
)
@when(
    parsers.parse(
        'the client waits up to {timeout:d} seconds for captured webhook event "{event}"'
    )
)
@then(
    parsers.parse(
        'the client waits up to {timeout:d} seconds for captured webhook event "{event}"'
    )
)
@given(parsers.parse('the client waits for captured webhook event "{event}" delivery {delivery:d}'))
@when(parsers.parse('the client waits for captured webhook event "{event}" delivery {delivery:d}'))
@then(parsers.parse('the client waits for captured webhook event "{event}" delivery {delivery:d}'))
def when_the_client_waits_for_captured_webhook_event(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    event: str,
    delivery: int = 1,
    timeout: int = 5,
) -> None:
    acceptance_context.captured_webhook_payload = acceptance_system.wait_for_webhook_event(
        event,
        delivery=delivery,
        timeout=float(timeout),
    )


@given(parsers.parse('the client waits for captured webhook attempt "{event}" result "{result}"'))
@when(parsers.parse('the client waits for captured webhook attempt "{event}" result "{result}"'))
@then(parsers.parse('the client waits for captured webhook attempt "{event}" result "{result}"'))
@given(
    parsers.parse(
        'the client waits for captured webhook attempt "{event}" '
        'result "{result}" attempt {attempt:d}'
    )
)
@when(
    parsers.parse(
        'the client waits for captured webhook attempt "{event}" '
        'result "{result}" attempt {attempt:d}'
    )
)
@then(
    parsers.parse(
        'the client waits for captured webhook attempt "{event}" '
        'result "{result}" attempt {attempt:d}'
    )
)
def when_the_client_waits_for_captured_webhook_attempt(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    event: str,
    result: str,
    attempt: int = 1,
) -> None:
    acceptance_context.captured_webhook_attempt = acceptance_system.wait_for_webhook_attempt(
        event,
        result=result,
        attempt=attempt,
    )


@when(parsers.parse('the operator uploads collection source "{collection_id}" with arc'))
def when_operator_uploads_collection_source(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    source_root = acceptance_system.collection_source_root(collection_id)
    acceptance_context.command_text = f'arc upload {collection_id} "{source_root}"'
    acceptance_context.command_argv = [
        "arc",
        "upload",
        collection_id,
        str(source_root),
    ]
    acceptance_context.stdout_json = None
    acceptance_context.expected_api_endpoint = None
    acceptance_context.expected_api_payload = None
    acceptance_context.command = acceptance_system.run_arc(
        "upload",
        collection_id,
        str(acceptance_system.collection_source_root(collection_id)),
    )


@when(parsers.parse("the operator runs '{command}'"))
def when_operator_runs_command(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    command: str,
) -> None:
    argv = shlex.split(command)
    acceptance_context.command_text = command
    acceptance_context.command_argv = argv
    acceptance_context.stdout_json = None
    acceptance_context.expected_api_endpoint = None
    acceptance_context.expected_api_payload = None

    if argv[0] == "arc":
        acceptance_context.command = acceptance_system.run_arc(*argv[1:])
        _prepare_arc_expectation(acceptance_system, acceptance_context)
        return

    if argv[0] == "arc-disc":
        acceptance_context.command = acceptance_system.run_arc_disc(*argv[1:])
        return

    raise AssertionError(f"unsupported command: {command}")


@when(parsers.parse('the operator runs arc-disc fetch "{fetch_id}"'))
def when_operator_runs_arc_disc_fetch(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
) -> None:
    _run_arc_disc_command(
        acceptance_system,
        acceptance_context,
        "fetch",
        fetch_id,
        "--device",
        _configured_optical_acceptance_device(),
    )


@when(parsers.parse('the operator runs arc-disc fetch "{fetch_id}" with JSON output'))
def when_operator_runs_arc_disc_fetch_json(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
) -> None:
    _run_arc_disc_command(
        acceptance_system,
        acceptance_context,
        "fetch",
        fetch_id,
        "--device",
        _configured_optical_acceptance_device(),
        "--json",
    )


@when("the operator runs arc-disc burn")
def when_operator_runs_arc_disc_burn(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    _run_arc_disc_command(
        acceptance_system,
        acceptance_context,
        "burn",
        "--device",
        _configured_optical_acceptance_device(),
    )


@when(parsers.parse('the operator runs arc-disc recover "{session_id}"'))
def when_operator_runs_arc_disc_recover(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    session_id: str,
) -> None:
    _run_arc_disc_command(
        acceptance_system,
        acceptance_context,
        "recover",
        session_id,
        "--device",
        _configured_optical_acceptance_device(),
    )


@then(parsers.parse("the response status is {status:d}"))
def then_response_status_is(
    acceptance_context: AcceptanceScenarioContext,
    status: int,
) -> None:
    assert _require_response(acceptance_context).status_code == status


@then(parsers.parse('the response header "{name}" is "{value}"'))
def then_response_header_is(
    acceptance_context: AcceptanceScenarioContext,
    name: str,
    value: str,
) -> None:
    response = _require_response(acceptance_context)
    assert response.headers.get(name) == value


@then(parsers.parse('the response has header "{name}"'))
def then_response_has_header(
    acceptance_context: AcceptanceScenarioContext,
    name: str,
) -> None:
    response = _require_response(acceptance_context)
    assert name in response.headers


@then(parsers.parse("the response status is {status:d} both times"))
def then_response_status_is_both_times(
    acceptance_context: AcceptanceScenarioContext,
    status: int,
) -> None:
    assert len(acceptance_context.responses) == 2
    assert [response.status_code for response in acceptance_context.responses] == [status, status]


@then(parsers.re(r'the response contains "(?P<first>[^"]+)"(?P<rest>.*)'))
def then_response_contains_fields(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert set([first, *_quoted_values(rest)]).issubset(payload)


@then(parsers.parse('the response contains collection id "{collection_id}"'))
def then_response_contains_collection_id(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection"]["id"] == collection_id


@then("the response contains the correct file count")
def then_response_contains_correct_file_count(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection"]["files"] == PHOTOS_2024_FILE_COUNT


@then("the response contains the correct total bytes")
def then_response_contains_correct_total_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection"]["bytes"] == PHOTOS_2024_TOTAL_BYTES


@then(parsers.parse('collection upload "{collection_id}" state is "{state}"'))
def then_collection_upload_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection_id"] == collection_id
    assert payload["state"] == state


@then(parsers.parse('collection upload "{collection_id}" file "{path}" is "{state}"'))
def then_collection_upload_file_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    path: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection_id"] == collection_id
    file_payload = next(item for item in payload["files"] if item["path"] == path)
    assert file_payload["upload_state"] == state


@then(parsers.parse('collection upload "{collection_id}" reports uploaded bytes 0 for every file'))
def then_collection_upload_reports_zero_uploaded_bytes(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection_id"] == collection_id
    assert all(int(item["uploaded_bytes"]) == 0 for item in payload["files"])


@then(parsers.parse('collection upload "{collection_id}" latest failure contains "{text}"'))
def then_collection_upload_latest_failure_contains(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    text: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["collection_id"] == collection_id
    assert text in str(payload.get("latest_failure"))


@then(parsers.parse('fetch manifest entry "{entry_id}" upload state is "{state}"'))
def then_fetch_manifest_entry_upload_state_is(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    entry = next(item for item in payload["entries"] if item["id"] == entry_id)
    assert entry["upload_state"] == state


@then(parsers.parse('fetch manifest entry "{entry_id}" uploaded bytes is {count:d}'))
def then_fetch_manifest_entry_uploaded_bytes_is(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
    count: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    entry = next(item for item in payload["entries"] if item["id"] == entry_id)
    assert int(entry["uploaded_bytes"]) == count


@then(parsers.parse('collection "{collection_id}" is not yet visible'))
def then_collection_is_not_yet_visible(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    response = acceptance_system.request("GET", f"/v1/collections/{quote(collection_id, safe='/')}")
    if response.status_code == 200:
        payload = (
            _json_payload(acceptance_context.response)
            if acceptance_context.response is not None
            else {}
        )
        if (
            payload.get("collection_id") == collection_id
            and payload.get("state") == "archiving"
        ):
            return
    assert response.status_code == 404


@then(parsers.parse('collection "{collection_id}" is not eligible for planning'))
def then_collection_is_not_eligible_for_planning(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    response = acceptance_system.request("GET", "/v1/plan", params={"collection": collection_id})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


@then(parsers.parse('collection "{collection_id}" has hot_bytes equal to bytes'))
def then_collection_hot_bytes_equal_bytes(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    payload = acceptance_system.collections.get(collection_id)
    assert payload.hot_bytes == payload.bytes


@then(parsers.parse('collection "{collection_id}" has archived_bytes equal to 0'))
def then_collection_archived_bytes_equal_zero(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    payload = acceptance_system.collections.get(collection_id)
    assert payload.archived_bytes == 0


@then(parsers.parse('collection "{collection_id}" has pending_bytes equal to bytes'))
def then_collection_pending_bytes_equal_bytes(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    payload = acceptance_system.collections.get(collection_id)
    assert payload.pending_bytes == payload.bytes


@then(parsers.parse('collection "{collection_id}" is eligible for planning'))
def then_collection_is_eligible_for_planning(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> None:
    payload = acceptance_system.collections.get(collection_id)
    assert payload.pending_bytes > 0


@then(parsers.parse('collection "{collection_id}" glacier state is "{state}"'))
def then_collection_glacier_state_is(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    state: str,
) -> None:
    response = acceptance_system.request(
        "GET",
        f"/v1/collections/{quote(collection_id, safe='/')}",
    )
    assert response.status_code == 200, response.text
    assert response.json()["glacier"]["state"] == state


@then(parsers.parse('collection "{collection_id}" archive manifest state is "{state}"'))
def then_named_collection_archive_manifest_state_is(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    state: str,
) -> None:
    response = acceptance_system.request(
        "GET",
        f"/v1/collections/{quote(collection_id, safe='/')}",
    )
    assert response.status_code == 200, response.text
    manifest = response.json()["archive_manifest"]
    assert manifest is not None
    assert ("uploaded" if manifest["object_path"] else "pending") == state


@then(parsers.parse('collection "{collection_id}" OTS proof state is "{state}"'))
def then_named_collection_ots_proof_state_is(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    state: str,
) -> None:
    response = acceptance_system.request(
        "GET",
        f"/v1/collections/{quote(collection_id, safe='/')}",
    )
    assert response.status_code == 200, response.text
    manifest = response.json()["archive_manifest"]
    assert manifest is not None
    assert manifest["ots_state"] == state


@then("pending_bytes equals bytes minus archived_bytes")
def then_pending_bytes_equals_bytes_minus_archived_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["pending_bytes"] == payload["bytes"] - payload["archived_bytes"]


@then("hot_bytes is between 0 and bytes")
def then_hot_bytes_is_between_zero_and_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert 0 <= payload["hot_bytes"] <= payload["bytes"]


@then("archived_bytes is between 0 and bytes")
def then_archived_bytes_is_between_zero_and_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert 0 <= payload["archived_bytes"] <= payload["bytes"]


@then(parsers.parse('collection protection_state is "{state}"'))
def then_collection_protection_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["protection_state"] == state


@then(parsers.parse("protected_bytes is {count:d}"))
def then_protected_bytes_is(
    acceptance_context: AcceptanceScenarioContext,
    count: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["protected_bytes"] == count


@then("protected_bytes equals bytes")
def then_protected_bytes_equals_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["protected_bytes"] == payload["bytes"]


@then(parsers.parse('collection verified_physical recovery state is "{state}"'))
def then_collection_verified_physical_recovery_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["recovery"]["verified_physical"]["state"] == state, payload["recovery"]


@then(parsers.parse('collection Glacier recovery state is "{state}"'))
def then_collection_glacier_recovery_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["recovery"]["glacier"]["state"] == state, payload["recovery"]


@then(parsers.parse('collection glacier state is "{state}"'))
def then_response_collection_glacier_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["glacier"]["state"] == state


@then(parsers.parse('collection archive manifest state is "{state}"'))
def then_response_collection_archive_manifest_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    manifest = payload["archive_manifest"]
    assert manifest is not None
    assert ("uploaded" if manifest["object_path"] else "pending") == state


@then(parsers.parse('collection OTS proof state is "{state}"'))
def then_response_collection_ots_proof_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    manifest = payload["archive_manifest"]
    assert manifest is not None
    assert manifest["ots_state"] == state


@then(parsers.parse('collection disc coverage state is "{state}"'))
def then_response_collection_disc_coverage_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["disc_coverage"]["state"] == state, payload


@then(parsers.parse('collection image coverage includes image "{image_id}"'))
def then_collection_image_coverage_includes_image(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [image["id"] for image in payload["image_coverage"]]
    assert image_id in ids


@then(parsers.parse('collection image coverage for image "{image_id}" includes copy "{copy_id}"'))
def then_collection_image_coverage_for_image_includes_copy(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
    copy_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    image = next(image for image in payload["image_coverage"] if image["id"] == image_id)
    assert copy_id in [copy["id"] for copy in image["copies"]]


@then(parsers.parse('collection image coverage for image "{image_id}" includes path "{path}"'))
def then_collection_image_coverage_for_image_includes_path(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
    path: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    image = next(image for image in payload["image_coverage"] if image["id"] == image_id)
    assert path in image["covered_paths"]


@then(parsers.parse('the response query is "{query}"'))
def then_response_query_is(
    acceptance_context: AcceptanceScenarioContext,
    query: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["query"] == query


@then("the response contains at least one file result")
def then_response_contains_file_results(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    file_results = [item for item in payload["results"] if item["kind"] == "file"]
    assert file_results


@then("each file result contains a canonical target")
@then("each file result contains a canonical selector")
@then("each file result contains a projected-path selector")
def then_each_file_result_contains_canonical_target(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    file_results = [item for item in payload["results"] if item["kind"] == "file"]
    assert file_results
    assert all(result["target"] for result in file_results)
    assert all(":" not in str(result["target"]) for result in file_results)
    assert all(not str(result["target"]).startswith("/") for result in file_results)


@then("each file result contains current hot availability")
def then_each_file_result_contains_hot_availability(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    file_results = [item for item in payload["results"] if item["kind"] == "file"]
    assert all("hot" in result for result in file_results)


@then(parsers.re(r'each file entry contains "(?P<first>[^"]+)"(?P<rest>.*)'))
def then_each_file_entry_contains_fields(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    expected = {first, *_quoted_values(rest)}
    for entry in payload["files"]:
        assert expected.issubset(entry)


@then("the response files list has exactly 1 entry")
def then_response_files_list_has_one(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert len(payload["files"]) == 1


@then("the response files list is empty")
def then_response_files_list_is_empty(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["files"] == []


@then("the response files list is non-empty")
def then_response_files_list_is_non_empty(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["files"]


@then("every file entry has hot equal to true")
def then_every_file_entry_is_hot(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert all(entry["hot"] for entry in payload["files"])


@then(parsers.parse('the response content type is "{content_type}"'))
def then_response_content_type_is(
    acceptance_context: AcceptanceScenarioContext,
    content_type: str,
) -> None:
    response = _require_response(acceptance_context)
    assert content_type in response.headers.get("content-type", "")


@then("each file result contains available copies if archived")
def then_each_file_result_contains_copies_if_archived(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    file_results = [item for item in payload["results"] if item["kind"] == "file"]
    assert all("copies" in result for result in file_results)
    assert all(result["copies"] for result in file_results if result["hot"] is False)


@then("every returned target is valid input for pin")
def then_every_returned_target_is_valid_input_for_pin(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    for target in [item["target"] for item in payload["results"]]:
        assert (
            acceptance_system.request("POST", "/v1/pin", json_body={"target": target}).status_code
            == 200
        )


@then("every returned target is valid input for release")
def then_every_returned_target_is_valid_input_for_release(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    for target in [item["target"] for item in payload["results"]]:
        assert (
            acceptance_system.request(
                "POST", "/v1/release", json_body={"target": target}
            ).status_code
            == 200
        )


@then(parsers.parse("the response contains at most {limit:d} result"))
def then_response_contains_at_most_limit(
    acceptance_context: AcceptanceScenarioContext,
    limit: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert len(payload["results"]) <= limit


@then(parsers.parse('the response contains target "{target}"'))
def then_response_contains_target(
    acceptance_context: AcceptanceScenarioContext,
    target: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    if "results" in payload:
        assert target in [item["target"] for item in payload["results"]]
        return
    assert payload["target"] == target


@then("pin is true")
def then_pin_is_true(acceptance_context: AcceptanceScenarioContext) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["pin"] is True


@then("pin is false")
def then_pin_is_false(acceptance_context: AcceptanceScenarioContext) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["pin"] is False


@then(parsers.parse('hot state is "{state}"'))
def then_hot_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["hot"]["state"] == state


@then("missing_bytes is 0")
def then_missing_bytes_is_zero(acceptance_context: AcceptanceScenarioContext) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["hot"]["missing_bytes"] == 0


@then("missing_bytes is greater than 0")
def then_missing_bytes_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["hot"]["missing_bytes"] > 0


@given(parsers.parse('the client has already pinned "{target}"'))
def given_client_has_already_pinned(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    target: str,
) -> None:
    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": target})
    assert response.status_code == 200, response.text
    acceptance_context.last_fetch_id = response.json()["fetch"]["id"]


@then("a fetch id is returned")
def then_fetch_id_is_returned(acceptance_context: AcceptanceScenarioContext) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    fetch_id = payload["fetch"]["id"]
    assert fetch_id
    acceptance_context.last_fetch_id = str(fetch_id)


@then("the returned fetch id is the same as before")
def then_returned_fetch_id_is_same_as_before(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["fetch"]["id"] == acceptance_context.last_fetch_id


@when("the client gets the manifest for the returned fetch")
def when_client_gets_manifest_for_returned_fetch(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert acceptance_context.last_fetch_id is not None, "no fetch id was captured"
    response = acceptance_system.request(
        "GET", f"/v1/fetches/{acceptance_context.last_fetch_id}/manifest"
    )
    _set_response(acceptance_context, response)


@then(parsers.parse('fetch manifest entry "{entry_id}" has at least one copy with a disc_path'))
def then_fetch_manifest_entry_has_copies_with_disc_path(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
) -> None:
    entry = _response_manifest_entry(acceptance_context, entry_id)
    copies: list[object] = []
    for part in entry.get("parts", []):
        copies.extend(part.get("copies", []))  # type: ignore[arg-type]
    if not copies:
        copies = list(entry.get("copies", []))
    assert copies, f"manifest entry {entry_id} has no copies"
    assert all(c.get("disc_path") for c in copies), (  # type: ignore[union-attr]
        f"manifest entry {entry_id} has copies with missing disc_path"
    )


@then("fetch is null")
def then_fetch_is_null(acceptance_context: AcceptanceScenarioContext) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["fetch"] is None


@then(parsers.parse('fetch state is "{state}"'))
def then_fetch_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    if "fetch" in payload and payload["fetch"] is not None:
        assert payload["fetch"]["state"] == state
        return
    assert payload["state"] == state


@then(parsers.parse('the returned fetch id is "{fetch_id}"'))
def then_returned_fetch_id_is(
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["fetch"]["id"] == fetch_id


@then(parsers.parse('"/v1/pins" contains target "{target}" exactly once'))
def then_pins_contains_target_exactly_once(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    pins = [item["target"] for item in acceptance_system.request("GET", "/v1/pins").json()["pins"]]
    assert pins.count(target) == 1


@then(parsers.parse('"/v1/pins" does not contain target "{target}"'))
def then_pins_does_not_contain_target(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    pins = [item["target"] for item in acceptance_system.request("GET", "/v1/pins").json()["pins"]]
    assert target not in pins


@then(parsers.parse('"/v1/pins" still contains target "{target}"'))
def then_pins_still_contains_target(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    pins = [item["target"] for item in acceptance_system.request("GET", "/v1/pins").json()["pins"]]
    assert target in pins


def _pin_entry(acceptance_system: AcceptanceSystem, target: str) -> dict[str, object]:
    pins = acceptance_system.request("GET", "/v1/pins").json()["pins"]
    for entry in pins:
        if entry["target"] == target:
            return entry
    raise AssertionError(f'pin entry not found for target "{target}"')


@then(parsers.parse('"/v1/pins" entry for target "{target}" contains fetch id "{fetch_id}"'))
def then_pins_entry_contains_fetch_id(
    acceptance_system: AcceptanceSystem,
    target: str,
    fetch_id: str,
) -> None:
    assert _pin_entry(acceptance_system, target)["fetch"]["id"] == fetch_id


@then(parsers.parse('"/v1/pins" entry for target "{target}" contains fetch state "{state}"'))
def then_pins_entry_contains_fetch_state(
    acceptance_system: AcceptanceSystem,
    target: str,
    state: str,
) -> None:
    assert _pin_entry(acceptance_system, target)["fetch"]["state"] == state


@then(parsers.parse('file "{target}" remains hot'))
@then(parsers.parse('target "{target}" is hot'))
def then_target_is_hot(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert bool(files) and all(record["hot"] for record in files)


@then(parsers.parse('collection "{collection_id}" does not have committed file "{path}"'))
def then_collection_does_not_have_committed_file(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
    path: str,
) -> None:
    assert not acceptance_system.has_committed_collection_file(collection_id, path)


@then(parsers.parse('file "{target}" is not hot'))
@then(parsers.parse('target "{target}" is not hot'))
def then_target_is_not_hot(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert not (bool(files) and all(record["hot"] for record in files))


@when("the client lists the read-only browsing root")
def when_the_client_lists_the_read_only_browsing_root(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    acceptance_context.read_only_browsing_paths = acceptance_system.list_read_only_browsing_paths()


@then(parsers.parse('the read-only browsing surface exposes path "{path}"'))
def then_the_read_only_browsing_surface_exposes_path(
    acceptance_context: AcceptanceScenarioContext,
    path: str,
) -> None:
    assert path in acceptance_context.read_only_browsing_paths


@then(parsers.parse('the read-only browsing surface hides path "{path}"'))
def then_the_read_only_browsing_surface_hides_path(
    acceptance_context: AcceptanceScenarioContext,
    path: str,
) -> None:
    assert path not in acceptance_context.read_only_browsing_paths


@when(parsers.parse('the client attempts to write "{path}" through the read-only browsing surface'))
def when_the_client_attempts_to_write_through_the_read_only_browsing_surface(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    path: str,
) -> None:
    acceptance_context.response = acceptance_system.write_through_read_only_browsing_surface(path)


@then("the read-only browsing write is rejected")
def then_the_read_only_browsing_write_is_rejected(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert _require_response(acceptance_context).status_code >= 400


@when("the client inspects the canonical storage lifecycle configuration")
def when_the_client_inspects_the_canonical_storage_lifecycle_configuration(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    acceptance_context.stdout_json = acceptance_system.storage_lifecycle_configuration(
        storage="hot"
    )


@when("the client inspects the canonical archive-storage lifecycle configuration")
def when_the_client_inspects_the_canonical_archive_storage_lifecycle_configuration(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    acceptance_context.stdout_json = acceptance_system.storage_lifecycle_configuration(
        storage="archive"
    )


@then("the storage lifecycle aborts incomplete multipart uploads after 3 days")
def then_the_storage_lifecycle_aborts_incomplete_multipart_uploads_after_3_days(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = acceptance_context.stdout_json
    assert isinstance(payload, dict)
    rules = payload.get("Rules", [])
    assert isinstance(rules, list) and rules
    first = rules[0]
    assert isinstance(first, dict)
    assert first.get("ID") == "abort-incomplete-riverhog-uploads"
    assert first.get("Status") == "Enabled"
    abort = first.get("AbortIncompleteMultipartUpload")
    assert isinstance(abort, dict)
    assert abort.get("DaysAfterInitiation") == 3


@then(parsers.parse('the {storage} bucket contains object "{key}"'))
def then_bucket_contains_object(
    acceptance_system: AcceptanceSystem,
    storage: str,
    key: str,
) -> None:
    assert acceptance_system.bucket_contains_object(storage=storage, key=key)


@then(parsers.parse('the {storage} bucket object "{key}" records validated ISO metadata'))
def then_bucket_object_records_validated_iso_metadata(
    acceptance_system: AcceptanceSystem,
    storage: str,
    key: str,
) -> None:
    metadata = acceptance_system.bucket_object_metadata(storage=storage, key=key)
    iso_bytes = metadata.get("arc-iso-bytes")
    iso_sha256 = metadata.get("arc-iso-sha256")
    assert iso_bytes is not None and int(iso_bytes) > 0
    assert iso_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", iso_sha256)


def _collection_archive_object_path(
    acceptance_system: AcceptanceSystem,
    collection_id: str,
) -> str:
    response = acceptance_system.request(
        "GET",
        f"/v1/collections/{quote(collection_id, safe='/')}",
    )
    assert response.status_code == 200, response.text
    return str(response.json()["glacier"]["object_path"])


@then(
    parsers.parse(
        'the {storage} bucket contains collection Glacier archive package for collection '
        '"{collection_id}"'
    )
)
def then_bucket_contains_collection_glacier_archive_package(
    acceptance_system: AcceptanceSystem,
    storage: str,
    collection_id: str,
) -> None:
    assert acceptance_system.bucket_contains_object(
        storage=storage,
        key=_collection_archive_object_path(acceptance_system, collection_id),
    )


@then(
    parsers.parse(
        'the {storage} bucket does not contain collection Glacier archive package for collection '
        '"{collection_id}"'
    )
)
def then_bucket_does_not_contain_collection_glacier_archive_package(
    acceptance_system: AcceptanceSystem,
    storage: str,
    collection_id: str,
) -> None:
    assert not acceptance_system.bucket_contains_object(
        storage=storage,
        key=_collection_archive_object_path(acceptance_system, collection_id),
    )


@then(
    parsers.parse(
        'the {storage} bucket object for collection "{collection_id}" '
        "records validated archive metadata"
    )
)
def then_bucket_object_for_collection_records_archive_metadata(
    acceptance_system: AcceptanceSystem,
    storage: str,
    collection_id: str,
) -> None:
    metadata = acceptance_system.bucket_object_metadata(
        storage=storage,
        key=_collection_archive_object_path(acceptance_system, collection_id),
    )
    archive_bytes = metadata.get("arc-archive-bytes")
    archive_sha256 = metadata.get("arc-archive-sha256")
    assert archive_bytes is not None and int(archive_bytes) > 0
    assert archive_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", archive_sha256)


@then(
    parsers.parse(
        'the {credentials} credentials cannot read collection Glacier archive package '
        'for collection "{collection_id}" from the {storage} bucket'
    )
)
def then_credentials_cannot_read_collection_glacier_archive_package(
    acceptance_system: AcceptanceSystem,
    credentials: str,
    collection_id: str,
    storage: str,
) -> None:
    assert acceptance_system.bucket_read_is_rejected(
        credentials=credentials,
        storage=storage,
        key=_collection_archive_object_path(acceptance_system, collection_id),
    )


@then(parsers.parse('the {storage} bucket does not contain object "{key}"'))
def then_bucket_does_not_contain_object(
    acceptance_system: AcceptanceSystem,
    storage: str,
    key: str,
) -> None:
    assert not acceptance_system.bucket_contains_object(storage=storage, key=key)


@then(parsers.parse('the {storage} bucket contains prefix "{prefix}"'))
def then_bucket_contains_prefix(
    acceptance_system: AcceptanceSystem,
    storage: str,
    prefix: str,
) -> None:
    assert acceptance_system.bucket_contains_prefix(storage=storage, prefix=prefix)


@then(parsers.parse('the {storage} bucket does not contain prefix "{prefix}"'))
def then_bucket_does_not_contain_prefix(
    acceptance_system: AcceptanceSystem,
    storage: str,
    prefix: str,
) -> None:
    assert not acceptance_system.bucket_contains_prefix(storage=storage, prefix=prefix)


@then(
    parsers.parse(
        'the {credentials} credentials cannot write object "{key}" to the {storage} bucket'
    )
)
def then_credentials_cannot_write_object_to_bucket(
    acceptance_system: AcceptanceSystem,
    credentials: str,
    storage: str,
    key: str,
) -> None:
    assert acceptance_system.bucket_write_is_rejected(
        credentials=credentials,
        storage=storage,
        key=key,
    )


@then(
    parsers.parse(
        'the {credentials} credentials cannot read object "{key}" from the {storage} bucket'
    )
)
def then_credentials_cannot_read_object_from_bucket(
    acceptance_system: AcceptanceSystem,
    credentials: str,
    storage: str,
    key: str,
) -> None:
    assert acceptance_system.bucket_read_is_rejected(
        credentials=credentials,
        storage=storage,
        key=key,
    )


@then(
    parsers.parse(
        'the {credentials} credentials cannot list prefix "{prefix}" in the {storage} bucket'
    )
)
def then_credentials_cannot_list_prefix_in_bucket(
    acceptance_system: AcceptanceSystem,
    credentials: str,
    storage: str,
    prefix: str,
) -> None:
    assert acceptance_system.bucket_list_is_rejected(
        credentials=credentials,
        storage=storage,
        prefix=prefix,
    )


@then(parsers.parse('target "{target}" is pinned'))
@then(parsers.parse('target "{target}" remains pinned'))
def then_target_is_pinned(
    acceptance_system: AcceptanceSystem,
    target: str,
) -> None:
    assert target in acceptance_system.pins_list()


@then(parsers.parse('the error code is "{code}"'))
def then_error_code_is(
    acceptance_context: AcceptanceScenarioContext,
    code: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["error"]["code"] == code


@then(parsers.parse('the error message contains "{text}"'))
def then_error_message_contains(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert text in payload["error"]["message"]


@then(
    'each plan candidate contains "candidate_id", "bytes", "fill", "files", '
    '"collections", "collection_ids", and "iso_ready"'
)
def then_each_plan_candidate_contains_expected_fields(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    expected = {
        "candidate_id",
        "bytes",
        "fill",
        "files",
        "collections",
        "collection_ids",
        "iso_ready",
    }
    assert payload["candidates"]
    assert all(expected.issubset(candidate) for candidate in payload["candidates"])


@then(parsers.parse('plan candidates do not contain field "{field}"'))
def then_plan_candidates_do_not_contain_field(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["candidates"]
    assert all(field not in candidate for candidate in payload["candidates"])


@then("each candidate fill equals candidate bytes divided by target bytes")
def then_each_candidate_fill_matches_target_bytes(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    target_bytes = payload["target_bytes"]
    for candidate in payload["candidates"]:
        assert candidate["fill"] == candidate["bytes"] / target_bytes


@then("candidates are returned fullest-first")
def then_candidates_are_returned_fullest_first(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    fills = [candidate["fill"] for candidate in payload["candidates"]]
    assert fills == sorted(fills, reverse=True)


@then("plan candidates are returned by candidate_id ascending")
def then_plan_candidates_are_returned_by_candidate_id_ascending(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [candidate["candidate_id"] for candidate in payload["candidates"]]
    assert ids == sorted(ids)


@then(parsers.parse("the response contains {count:d} plan candidates"))
def then_response_contains_plan_candidate_count(
    acceptance_context: AcceptanceScenarioContext,
    count: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert len(payload["candidates"]) == count


@then(parsers.parse("the response contains {count:d} collection summaries"))
def then_response_contains_collection_summaries_count(
    acceptance_context: AcceptanceScenarioContext,
    count: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert len(payload["collections"]) == count


@then(parsers.parse('the response collection summaries contain only "{collection_id}"'))
def then_response_collection_summaries_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [item["id"] for item in payload["collections"]] == [collection_id]


@then(parsers.re(r'the response plan candidates include "(?P<first>[^"]+)"(?P<rest>.*)'))
def then_response_plan_candidates_include(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [candidate["candidate_id"] for candidate in payload["candidates"]]
    for candidate_id in [first, *_quoted_values(rest)]:
        assert candidate_id in ids


@then(parsers.parse('the response plan candidates contain only "{candidate_id}"'))
def then_response_plan_candidates_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    candidate_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [candidate["candidate_id"] for candidate in payload["candidates"]] == [candidate_id]


@then(
    'each finalized image contains "id", "filename", "finalized_at", "bytes", '
    '"fill", "files", "collections", "collection_ids", "iso_ready", '
    '"physical_protection_state", "physical_copies_required", '
    '"physical_copies_registered", "physical_copies_verified", and '
    '"physical_copies_missing"'
)
def then_each_finalized_image_contains_expected_fields(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    expected = {
        "id",
        "filename",
        "finalized_at",
        "bytes",
        "fill",
        "files",
        "collections",
        "collection_ids",
        "iso_ready",
        "physical_protection_state",
        "physical_copies_required",
        "physical_copies_registered",
        "physical_copies_verified",
        "physical_copies_missing",
    }
    assert all(expected.issubset(image) for image in payload["images"])
    assert all("protection_state" not in image for image in payload["images"])
    assert all("glacier" not in image for image in payload["images"])


@then("finalized images are returned newest-first")
def then_finalized_images_are_returned_newest_first(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [image["id"] for image in payload["images"]]
    assert ids == sorted(ids, reverse=True)


@then("each finalized image is iso-ready")
def then_each_finalized_image_is_iso_ready(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert all(image["iso_ready"] is True for image in payload["images"])


@then(parsers.parse("the response contains {count:d} finalized images"))
def then_response_contains_finalized_image_count(
    acceptance_context: AcceptanceScenarioContext,
    count: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert len(payload["images"]) == count


@then(
    parsers.parse(
        "the response pagination is page {page:d} with per_page {per_page:d} "
        "and total {total:d} and pages {pages:d}"
    )
)
def then_response_pagination_matches(
    acceptance_context: AcceptanceScenarioContext,
    page: int,
    per_page: int,
    total: int,
    pages: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["page"] == page
    assert payload["per_page"] == per_page
    assert payload["total"] == total
    assert payload["pages"] == pages


@then(parsers.re(r'the response finalized images include "(?P<first>[^"]+)"(?P<rest>.*)'))
def then_response_finalized_images_include(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [image["id"] for image in payload["images"]]
    for image_id in [first, *_quoted_values(rest)]:
        assert image_id in ids


@then(parsers.parse('the response finalized images contain only "{image_id}"'))
def then_response_finalized_images_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [image["id"] for image in payload["images"]] == [image_id]


@then("each finalized image has physical_copies_registered greater than 0")
def then_each_finalized_image_has_physical_copies_registered(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["images"]
    assert all(image["physical_copies_registered"] > 0 for image in payload["images"])


@then(parsers.parse('the response image physical_protection_state is "{state}"'))
def then_response_image_physical_protection_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["physical_protection_state"] == state


@then(parsers.parse('the response collection glacier object_path is under "{prefix}"'))
def then_response_collection_glacier_object_path_is_under(
    acceptance_context: AcceptanceScenarioContext,
    prefix: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    object_path = str(payload["glacier"]["object_path"])
    assert object_path.startswith(prefix)


@then(parsers.parse('the response recovery session id is "{session_id}"'))
def then_response_recovery_session_id_is(
    acceptance_context: AcceptanceScenarioContext,
    session_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["id"] == session_id


@then(parsers.parse('the response recovery session state is "{state}"'))
def then_response_recovery_session_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["state"] == state


@then(parsers.parse('the response recovery session type is "{session_type}"'))
def then_response_recovery_session_type_is(
    acceptance_context: AcceptanceScenarioContext,
    session_type: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["type"] == session_type


@then("the response recovery session estimated cost is greater than 0")
def then_response_recovery_session_estimated_cost_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert float(payload["cost_estimate"]["total_estimated_cost_usd"]) > 0


@then(parsers.parse('the response recovery session images contain only "{image_id}"'))
def then_response_recovery_session_images_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [image["id"] for image in payload["images"]] == [image_id]


@then("the response recovery session images are empty")
def then_response_recovery_session_images_are_empty(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["images"] == []


@then(parsers.parse('the response recovery session collections contain only "{collection_id}"'))
def then_response_recovery_session_collections_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [collection["id"] for collection in payload["collections"]] == [collection_id]


@then(parsers.parse('the response recovery session collections include "{collection_id}"'))
def then_response_recovery_session_collections_include(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert collection_id in [collection["id"] for collection in payload["collections"]]


@then(
    parsers.parse(
        'the response recovery session collection "{collection_id}" glacier state is "{state}"'
    )
)
def then_response_recovery_session_collection_glacier_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    assert collection["glacier"]["state"] == state


@then(
    parsers.parse(
        'the response recovery session collection "{collection_id}" '
        'archive manifest state is "{state}"'
    )
)
def then_response_recovery_session_collection_manifest_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    manifest = collection["archive_manifest"]
    assert manifest is not None
    assert ("uploaded" if manifest["object_path"] else "pending") == state


@then(
    parsers.parse(
        'the response recovery session collection "{collection_id}" OTS proof state is "{state}"'
    )
)
def then_response_recovery_session_collection_ots_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    manifest = collection["archive_manifest"]
    assert manifest is not None
    assert manifest["ots_state"] == state


@then(
    parsers.parse(
        'the response recovery session image "{image_id}" rebuild_state is "{state}"'
    )
)
def then_response_recovery_session_image_rebuild_state_is(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    image = next(item for item in payload["images"] if item["id"] == image_id)
    assert image["rebuild_state"] == state


@then(parsers.parse('the response recovery session latest_message contains "{text}"'))
def then_response_recovery_session_latest_message_contains(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert text in str(payload["latest_message"])


@then(parsers.parse('the response recovery session archive_verification is "{state}"'))
def then_response_recovery_session_archive_verification_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["progress"]["archive_verification"] == state


@then(parsers.parse('the response recovery session extraction is "{state}"'))
def then_response_recovery_session_extraction_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["progress"]["extraction"] == state


@then(parsers.parse('the response recovery session materialization is "{state}"'))
def then_response_recovery_session_materialization_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["progress"]["materialization"] == state


@then(parsers.parse('the captured webhook payload field "{field}" equals "{value}"'))
def then_captured_webhook_payload_field_equals(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
    value: str,
) -> None:
    payload = _require_captured_webhook_payload(acceptance_context)
    assert str(payload[field]) == value


@then(parsers.parse('the captured webhook payload integer field "{field}" equals {value:d}'))
def then_captured_webhook_payload_integer_field_equals(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
    value: int,
) -> None:
    payload = _require_captured_webhook_payload(acceptance_context)
    assert int(payload[field]) == value


@then(parsers.parse('the captured webhook payload images contain only "{image_id}"'))
def then_captured_webhook_payload_images_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _require_captured_webhook_payload(acceptance_context)
    images = payload.get("images", [])
    assert isinstance(images, list)
    normalized_ids = [
        str(image.get("image_id"))
        for image in images
        if isinstance(image, dict) and image.get("image_id") is not None
    ]
    assert normalized_ids == [image_id]


@then(
    parsers.parse('captured webhook event "{event}" has {count:d} successful deliveries')
)
def then_captured_webhook_event_has_successful_deliveries(
    acceptance_system: AcceptanceSystem,
    event: str,
    count: int,
) -> None:
    deliveries = [
        payload
        for payload in acceptance_system.list_webhook_deliveries()
        if str(payload.get("event")) == event
    ]
    assert len(deliveries) == count


@then(
    parsers.parse('captured webhook event "{event}" has {count:d} attempts with result "{result}"')
)
def then_captured_webhook_event_has_attempts_with_result(
    acceptance_system: AcceptanceSystem,
    event: str,
    count: int,
    result: str,
) -> None:
    attempts = [
        payload
        for payload in acceptance_system.list_webhook_attempts()
        if str(payload.get("event")) == event and str(payload.get("result")) == result
    ]
    assert len(attempts) == count


@then(
    parsers.parse(
        'captured webhook attempt "{event}" result "{result}" '
        'attempt {attempt:d} happened at least '
        "{seconds:d} seconds after result "
        '"{other_result}" attempt {other_attempt:d}'
    )
)
def then_captured_webhook_attempt_happened_at_least_seconds_after_other_attempt(
    acceptance_system: AcceptanceSystem,
    event: str,
    result: str,
    attempt: int,
    seconds: int,
    other_result: str,
    other_attempt: int,
) -> None:
    def _matching_attempt(target_result: str, target_attempt: int) -> dict[str, object]:
        matches = [
            payload
            for payload in acceptance_system.list_webhook_attempts()
            if str(payload.get("event")) == event and str(payload.get("result")) == target_result
        ]
        assert len(matches) >= target_attempt
        return matches[target_attempt - 1]

    current = _matching_attempt(result, attempt)
    previous = _matching_attempt(other_result, other_attempt)
    current_at = datetime.fromisoformat(str(current["received_at"]).replace("Z", "+00:00"))
    previous_at = datetime.fromisoformat(str(previous["received_at"]).replace("Z", "+00:00"))
    assert (current_at - previous_at).total_seconds() >= seconds


@then(parsers.parse('the captured webhook attempt integer field "{field}" equals {value:d}'))
def then_captured_webhook_attempt_integer_field_equals(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
    value: int,
) -> None:
    attempt = _require_captured_webhook_attempt(acceptance_context)
    assert int(attempt[field]) == value


@then("the response Glacier totals measured_storage_bytes is greater than 0")
def then_response_glacier_totals_measured_storage_bytes_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert int(payload["totals"]["measured_storage_bytes"]) > 0


@then("the response Glacier totals uploaded_collections is greater than 0")
def then_response_glacier_totals_uploaded_collections_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert int(payload["totals"]["uploaded_collections"]) > 0


@then("the response Glacier totals estimated_monthly_cost_usd is greater than 0")
def then_response_glacier_totals_estimated_monthly_cost_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert float(payload["totals"]["estimated_monthly_cost_usd"]) > 0


@then(parsers.parse('the response Glacier images contain only "{image_id}"'))
def then_response_glacier_images_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [image["id"] for image in payload["images"]] == [image_id]


@then(parsers.parse('the response Glacier collection "{collection_id}" glacier state is "{state}"'))
def then_response_glacier_collection_glacier_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    assert collection["glacier"]["state"] == state


@then(
    parsers.parse(
        'the response Glacier collection "{collection_id}" '
        "measured_storage_bytes is greater than 0"
    )
)
def then_response_glacier_collection_measured_storage_bytes_is_greater_than_zero(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    assert int(collection["measured_storage_bytes"]) > 0


@then(
    parsers.parse(
        'the response Glacier collection "{collection_id}" archive manifest state is "{state}"'
    )
)
def then_response_glacier_collection_archive_manifest_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    manifest = collection["archive_manifest"]
    assert manifest is not None
    assert ("uploaded" if manifest["object_path"] else "pending") == state


@then(
    parsers.parse(
        'the response Glacier collection "{collection_id}" OTS proof state is "{state}"'
    )
)
def then_response_glacier_collection_ots_state_is(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    state: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    collection = next(item for item in payload["collections"] if item["id"] == collection_id)
    manifest = collection["archive_manifest"]
    assert manifest is not None
    assert manifest["ots_state"] == state


@then(parsers.parse('the response Glacier collections contain only "{collection_id}"'))
def then_response_glacier_collections_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [item["id"] for item in payload["collections"]] == [collection_id]


@then("the response Glacier billing surface exposes resource-level and manifest metadata")
def then_response_glacier_billing_surface_exposes_resource_level_and_manifest_metadata(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    billing = payload["billing"]
    assert isinstance(billing, dict)
    actuals = billing["actuals"]
    exports = billing["exports"]
    assert actuals["source"] == "aws_cost_explorer_resource"
    assert actuals["scope"] == "bucket"
    assert actuals["billing_view_arn"]
    assert actuals["periods"]
    assert exports["source"] == "aws_data_exports_s3"
    assert exports["export_arn"]
    assert exports["export_name"]
    assert exports["execution_id"]
    assert exports["manifest_key"]
    assert exports["billing_period"]
    assert int(exports["files_read"]) > 0
    assert exports["breakdowns"]


@then("stdout exposes Glacier billing resource-level and manifest metadata")
def then_stdout_exposes_glacier_billing_resource_level_and_manifest_metadata(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    params: dict[str, object] = {}
    collection = _arc_option_value(acceptance_context.command_argv, "--collection")
    if collection is not None:
        params["collection"] = collection
    payload = acceptance_system.request("GET", "/v1/glacier", params=params).json()
    assert isinstance(payload, dict)
    billing = payload["billing"]
    assert isinstance(billing, dict)
    stdout = _require_command(acceptance_context).stdout
    actuals = billing["actuals"]
    exports = billing["exports"]
    assert actuals["source"] == "aws_cost_explorer_resource"
    assert "source=aws_cost_explorer_resource scope=bucket" in stdout
    assert f"billing_view_arn: {actuals['billing_view_arn']}" in stdout
    assert exports["source"] == "aws_data_exports_s3"
    assert "source=aws_data_exports_s3 scope=bucket" in stdout
    assert f"export_arn: {exports['export_arn']}" in stdout
    assert f"export_name: {exports['export_name']}" in stdout
    assert f"execution_id: {exports['execution_id']}" in stdout
    assert f"manifest_key: {exports['manifest_key']}" in stdout
    assert f"billing_period: {exports['billing_period']}" in stdout
    assert f"files_read: {exports['files_read']}" in stdout


@then(parsers.parse('the response contains image id "{image_id}"'))
def then_response_contains_image_id(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["id"] == image_id


@then(parsers.parse('the response candidates do not contain candidate id "{image_id}"'))
def then_response_candidates_do_not_contain_candidate_id(
    acceptance_context: AcceptanceScenarioContext,
    image_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    ids = [candidate["candidate_id"] for candidate in payload["candidates"]]
    assert image_id not in ids


@then("the response body is binary ISO content")
def then_response_body_is_binary_iso_content(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    response = _require_response(acceptance_context)
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.content


@then(parsers.parse('the response contains copy id "{copy_id}"'))
def then_response_contains_copy_id(
    acceptance_context: AcceptanceScenarioContext,
    copy_id: str,
) -> None:
    assert _response_copy_payload(acceptance_context)["id"] == copy_id


@then(parsers.re(r'the response copy contains "(?P<first>[^"]+)"(?P<rest>.*)'))
def then_response_copy_contains_fields(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    assert set([first, *_quoted_values(rest)]).issubset(_response_copy_payload(acceptance_context))


@then(parsers.parse('the response copy state is "{state}"'))
def then_response_copy_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    assert _response_copy_payload(acceptance_context)["state"] == state


@then(parsers.parse('the response copy verification_state is "{state}"'))
def then_response_copy_verification_state_is(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    assert _response_copy_payload(acceptance_context)["verification_state"] == state


@then(parsers.parse('the response copies contain only "{first_copy_id}" and "{second_copy_id}"'))
def then_response_copies_contain_only(
    acceptance_context: AcceptanceScenarioContext,
    first_copy_id: str,
    second_copy_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert [copy["id"] for copy in payload["copies"]] == [first_copy_id, second_copy_id]


@then(
    parsers.re(
        r"the response copy history contains events "
        r'"(?P<first>[^"]+)"(?P<rest>.*) in order'
    )
)
def then_response_copy_history_contains_events(
    acceptance_context: AcceptanceScenarioContext,
    first: str,
    rest: str,
) -> None:
    history = _response_copy_payload(acceptance_context)["history"]
    assert [entry["event"] for entry in history] == [first, *_quoted_values(rest)]


@then(
    parsers.parse(
        'the response copy history entry {index:d} has event "{event}", '
        'state "{state}", verification_state "{verification_state}", '
        'and location "{location}"'
    )
)
def then_response_copy_history_entry_matches(
    acceptance_context: AcceptanceScenarioContext,
    index: int,
    event: str,
    state: str,
    verification_state: str,
    location: str,
) -> None:
    history = _response_copy_payload(acceptance_context)["history"]
    assert history[index - 1] == {
        "at": history[index - 1]["at"],
        "event": event,
        "state": state,
        "verification_state": verification_state,
        "location": location,
    }


@then(
    parsers.re(
        r'listed copy "(?P<copy_id>[^"]+)" history contains events '
        r'"(?P<first>[^"]+)"(?P<rest>.*) in order'
    )
)
def then_listed_copy_history_contains_events(
    acceptance_context: AcceptanceScenarioContext,
    copy_id: str,
    first: str,
    rest: str,
) -> None:
    history = _listed_copy_payload(acceptance_context, copy_id)["history"]
    assert [entry["event"] for entry in history] == [first, *_quoted_values(rest)]


@then(
    parsers.parse(
        'listed copy "{copy_id}" history entry {index:d} has event "{event}", '
        'state "{state}", verification_state "{verification_state}", '
        'and location "{location}"'
    )
)
def then_listed_copy_history_entry_matches(
    acceptance_context: AcceptanceScenarioContext,
    copy_id: str,
    index: int,
    event: str,
    state: str,
    verification_state: str,
    location: str,
) -> None:
    history = _listed_copy_payload(acceptance_context, copy_id)["history"]
    assert history[index - 1] == {
        "at": history[index - 1]["at"],
        "event": event,
        "state": state,
        "verification_state": verification_state,
        "location": location,
    }


@then(parsers.parse('the response does not contain field "{field}"'))
def then_response_does_not_contain_field(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert field not in payload


@then(parsers.parse('the response field "{field}" is null'))
def then_response_field_is_null(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload[field] is None


@then(parsers.parse('the response field "{field}" is {value:d}'))
def then_response_field_is_int(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
    value: int,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert int(payload[field]) == value


@then(parsers.parse('the response field "{field}" matches compact UTC timestamp'))
def then_response_field_matches_compact_utc_timestamp(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    value = payload[field]
    assert isinstance(value, str)
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", value)


@then(parsers.parse('both responses contain the same value for field "{field}"'))
def then_both_responses_contain_same_value_for_field(
    acceptance_context: AcceptanceScenarioContext,
    field: str,
) -> None:
    assert len(acceptance_context.responses) == 2
    payloads = [_json_payload(response) for response in acceptance_context.responses]
    assert payloads[0][field] == payloads[1][field]


@then(parsers.parse('collection "{collection_id}" archived_bytes increases'))
def then_collection_archived_bytes_increases(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    assert (
        acceptance_context.after_collections[collection_id]["archived_bytes"]
        > acceptance_context.before_collections[collection_id]["archived_bytes"]
    )


@then(parsers.parse('collection "{collection_id}" pending_bytes decreases'))
def then_collection_pending_bytes_decreases(
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
) -> None:
    assert (
        acceptance_context.after_collections[collection_id]["pending_bytes"]
        < acceptance_context.before_collections[collection_id]["pending_bytes"]
    )


@then("both manifests contain the same entry ids")
def then_both_manifests_contain_same_entry_ids(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert len(acceptance_context.responses) == 2
    first_entries = acceptance_context.responses[0].json()["entries"]
    second_entries = acceptance_context.responses[1].json()["entries"]
    assert [entry["id"] for entry in first_entries] == [entry["id"] for entry in second_entries]


@then("both manifests contain the same logical file set")
def then_both_manifests_contain_same_logical_file_set(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert len(acceptance_context.responses) == 2
    first_entries = acceptance_context.responses[0].json()["entries"]
    second_entries = acceptance_context.responses[1].json()["entries"]
    assert [entry["path"] for entry in first_entries] == [entry["path"] for entry in second_entries]


@then(parsers.parse('fetch manifest entry "{entry_id}" lists split parts 0 and 1'))
def then_fetch_manifest_entry_lists_split_parts(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
) -> None:
    entry = _response_manifest_entry(acceptance_context, entry_id)
    assert [part["index"] for part in entry["parts"]] == [0, 1]


@then(
    parsers.parse(
        'fetch manifest entry "{entry_id}" part {part_index:d} is recoverable from copy "{copy_id}"'
    )
)
def then_fetch_manifest_part_recovers_from_copy(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
    part_index: int,
    copy_id: str,
) -> None:
    entry = _response_manifest_entry(acceptance_context, entry_id)
    part = entry["parts"][part_index]
    assert [copy["copy"] for copy in part["copies"]] == [copy_id]


@then(
    parsers.parse(
        'fetch manifest entry "{entry_id}" part hashes and recovery-byte hashes '
        "match the published split fixture"
    )
)
def then_fetch_manifest_part_hashes_match_fixture(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
) -> None:
    entry = _response_manifest_entry(acceptance_context, entry_id)
    assert [part["bytes"] for part in entry["parts"]] == [len(part) for part in SPLIT_FILE_PARTS]
    assert [part["sha256"] for part in entry["parts"]] == [
        hashlib.sha256(part).hexdigest() for part in SPLIT_FILE_PARTS
    ]
    assert [part["recovery_bytes"] for part in entry["parts"]] == [
        len(fixture_encrypt_bytes(part)) for part in SPLIT_FILE_PARTS
    ]
    assert [part["copies"][0]["recovery_sha256"] for part in entry["parts"]] == [
        hashlib.sha256(fixture_encrypt_bytes(part)).hexdigest() for part in SPLIT_FILE_PARTS
    ]


@then(
    parsers.re(
        r'fetch manifest entry "(?P<entry_id>[^"]+)" contains "(?P<first>[^"]+)"(?P<rest>.*)'
    )
)
def then_fetch_manifest_entry_contains_fields(
    acceptance_context: AcceptanceScenarioContext,
    entry_id: str,
    first: str,
    rest: str,
) -> None:
    entry = _response_manifest_entry(acceptance_context, entry_id)
    assert set([first, *_quoted_values(rest)]).issubset(entry)


@then(parsers.parse("the command exits with code {exit_code:d}"))
def then_command_exits_with_code(
    acceptance_context: AcceptanceScenarioContext,
    exit_code: int,
) -> None:
    command = _require_command(acceptance_context)
    assert command.returncode == exit_code, (
        f"expected exit code {exit_code}, got {command.returncode}\n"
        f"stdout:\n{command.stdout}\n"
        f"stderr:\n{command.stderr}"
    )


@then("the command exits non-zero")
def then_command_exits_non_zero(acceptance_context: AcceptanceScenarioContext) -> None:
    assert _require_command(acceptance_context).returncode != 0


@then("stdout is valid JSON")
def then_stdout_is_valid_json(acceptance_context: AcceptanceScenarioContext) -> None:
    acceptance_context.stdout_json = json.loads(_require_command(acceptance_context).stdout)


@then(parsers.parse('stdout matches the structure of {method} "{path}"'))
def then_stdout_matches_expected_api_payload(
    acceptance_context: AcceptanceScenarioContext,
    method: str,
    path: str,
) -> None:
    assert acceptance_context.expected_api_endpoint == (method, path)
    actual = acceptance_context.stdout_json
    expected = acceptance_context.expected_api_payload
    if path == "/v1/glacier":
        actual = _normalized_glacier_payload(actual)
        expected = _normalized_glacier_payload(expected)
    assert actual == expected


def _normalized_glacier_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if "measured_at" in normalized:
        normalized["measured_at"] = "<normalized>"
    return normalized


@then(parsers.parse('stdout mentions target "{target}"'))
def then_stdout_mentions_target(
    acceptance_context: AcceptanceScenarioContext,
    target: str,
) -> None:
    assert target in _require_command(acceptance_context).stdout


@then(parsers.parse('stdout mentions "{text}"'))
def then_stdout_mentions_text(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    assert text in _require_command(acceptance_context).stdout


@then(parsers.parse('stdout does not mention "{text}"'))
def then_stdout_does_not_mention_text(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    assert text not in _require_command(acceptance_context).stdout


@then(parsers.parse('stdout mentions fetch id "{fetch_id}"'))
def then_stdout_mentions_fetch_id(
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
) -> None:
    assert fetch_id in _require_command(acceptance_context).stdout


@then("stdout mentions at least one candidate copy id")
def then_stdout_mentions_candidate_copy_id(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    fetch = acceptance_system.fetches.get("fx-1")
    stdout = _require_command(acceptance_context).stdout
    assert any(str(copy.id) in stdout for copy in fetch.copies)


@then(parsers.parse('stdout reports fetch state "{state}"'))
def then_stdout_reports_fetch_state(
    acceptance_context: AcceptanceScenarioContext,
    state: str,
) -> None:
    payload = acceptance_context.stdout_json
    assert isinstance(payload, dict)
    assert payload["state"] == state


@then(parsers.parse('target for fetch "{fetch_id}" is hot'))
def then_target_for_fetch_is_hot(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    fetch_resp = acceptance_system.request("GET", f"/v1/fetches/{fetch_id}")
    assert fetch_resp.status_code == 200, fetch_resp.text
    target = fetch_resp.json()["target"]
    files_resp = acceptance_system.request("GET", "/v1/files", params={"target": target})
    assert files_resp.status_code == 200, files_resp.text
    files = files_resp.json()["files"]
    assert bool(files) and all(record["hot"] for record in files)


@then(parsers.parse('image "{image_id}" has physical_copies_registered {count:d}'))
def then_image_has_physical_copies_registered(
    acceptance_system: AcceptanceSystem,
    image_id: str,
    count: int,
) -> None:
    resp = acceptance_system.request("GET", f"/v1/images/{image_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["physical_copies_registered"] == count


@then(parsers.parse('copy "{copy_id}" for image "{image_id}" state is "{state}"'))
def then_copy_for_image_state_is(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
    image_id: str,
    state: str,
) -> None:
    resp = acceptance_system.request("GET", f"/v1/images/{image_id}/copies")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["copies"]
    copy = next(item for item in payload if item["id"] == copy_id)
    assert copy["state"] == state


@then(
    parsers.parse(
        'copy "{copy_id}" for image "{image_id}" verification_state is "{verification_state}"'
    )
)
def then_copy_for_image_verification_state_is(
    acceptance_system: AcceptanceSystem,
    copy_id: str,
    image_id: str,
    verification_state: str,
) -> None:
    resp = acceptance_system.request("GET", f"/v1/images/{image_id}/copies")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["copies"]
    copy = next(item for item in payload if item["id"] == copy_id)
    assert copy["verification_state"] == verification_state


@then("the downloaded ISO passes ISO verification")
def then_downloaded_iso_passes_verification(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert _require_inspected_iso(acceptance_context).iso_path.is_file()


@then("the extracted ISO root matches the disc layout contract")
def then_extracted_iso_root_matches_disc_layout(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert_root_layout_contract(_require_inspected_iso(acceptance_context))


@then("the decrypted disc manifest matches the disc manifest contract")
def then_decrypted_disc_manifest_matches_contract(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    assert_contract_schema("disc-manifest.schema.json", inspected.disc_manifest)
    assert_disc_manifest_semantics(inspected.disc_manifest)


@then("every referenced collection manifest matches the collection hash manifest contract")
def then_every_collection_manifest_matches_contract(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    for collection in inspected.disc_manifest["collections"]:
        payload = decrypt_yaml_file(inspected.extract_root / collection["manifest"])
        assert_contract_schema("collection-hash-manifest.schema.json", payload)
        collection_id = str(collection["id"])
        request_path = f"/v1/collection-files/{quote(collection_id, safe='/')}"
        resp = acceptance_system.request("GET", request_path)
        assert resp.status_code == 200, resp.text
        expected_files = sorted(record["path"] for record in resp.json()["files"])
        assert_collection_manifest_semantics(
            payload,
            expected_collection_id=collection_id,
            expected_files=expected_files,
        )


@then("every referenced file sidecar matches the file sidecar contract")
def then_every_sidecar_matches_contract(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    for collection in inspected.disc_manifest["collections"]:
        collection_id = str(collection["id"])
        for file_entry in collection["files"]:
            expected_part: dict[str, int] | None = None
            sidecar_relpath: str
            if "parts" in file_entry:
                present = file_entry["parts"]["present"]
                assert len(present) == 1
                current_part = present[0]
                sidecar_relpath = str(current_part["sidecar"])
                expected_part = {
                    "index": int(current_part["index"]),
                    "count": int(file_entry["parts"]["count"]),
                }
            else:
                sidecar_relpath = str(file_entry["sidecar"])
            payload = decrypt_yaml_file(inspected.extract_root / sidecar_relpath)
            assert_contract_schema("file-sidecar.schema.json", payload)
            assert_sidecar_semantics(
                payload,
                expected_collection_id=collection_id,
                expected_path=str(file_entry["path"]),
                expected_bytes=int(file_entry["bytes"]),
                expected_sha256=str(file_entry["sha256"]),
                expected_part=expected_part,
            )


@then("the current ISO README documents split-file recovery")
def then_current_iso_readme_documents_split_recovery(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    readme = _require_inspected_iso(acceptance_context).readme
    assert "arc-disc" in readme
    assert "DISC.yml.age" in readme
    assert "multiple discs" in readme


@then(parsers.parse('the current ISO payload for "{target}" decrypts to the original plaintext'))
def then_current_iso_payload_decrypts_to_original_plaintext(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    target: str,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    _, file_entry = manifest_entry_by_path(
        inspected.disc_manifest,
        _selected_relpath_for_target(acceptance_system, target),
    )
    assert "object" in file_entry
    payload = payload_bytes(inspected.extract_root / file_entry["object"])
    expected = _selected_content_for_target(acceptance_system, target)
    assert payload == expected


@then(
    parsers.parse(
        'the current ISO lists split file "{relpath}" part {part_index:d} of {part_count:d}'
    )
)
def then_current_iso_lists_split_file_part(
    acceptance_context: AcceptanceScenarioContext,
    relpath: str,
    part_index: int,
    part_count: int,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    _, file_entry = manifest_entry_by_path(inspected.disc_manifest, relpath)
    present = file_entry["parts"]["present"]
    assert file_entry["parts"]["count"] == part_count
    assert [part["index"] for part in present] == [part_index]


@then(parsers.parse('the current split payload for "{relpath}" is recorded'))
def then_current_split_payload_is_recorded(
    acceptance_context: AcceptanceScenarioContext,
    relpath: str,
) -> None:
    inspected = _require_inspected_iso(acceptance_context)
    _, file_entry = manifest_entry_by_path(inspected.disc_manifest, relpath)
    present = file_entry["parts"]["present"]
    assert len(present) == 1
    current_part = present[0]
    payload = payload_bytes(inspected.extract_root / current_part["object"])
    acceptance_context.recorded_split_payloads.setdefault(relpath, {})[
        int(current_part["index"])
    ] = payload


@then(
    parsers.parse('the recorded split payloads for "{target}" reconstruct the original plaintext')
)
def then_recorded_split_payloads_reconstruct_plaintext(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    target: str,
) -> None:
    relpath = f"/{_selected_relpath_for_target(acceptance_system, target).lstrip('/')}"
    recorded = acceptance_context.recorded_split_payloads[relpath]
    reconstructed = b"".join(recorded[index] for index in sorted(recorded))
    expected = _selected_content_for_target(acceptance_system, target)
    assert reconstructed == expected


@then(parsers.parse('fetch "{fetch_id}" is not "{state}"'))
def then_fetch_is_not_state(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
    state: str,
) -> None:
    assert acceptance_system.fetches.get(fetch_id).state.value != state


@then(parsers.parse('fetch "{fetch_id}" no longer exists'))
def then_fetch_no_longer_exists(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    resp = acceptance_system.request("GET", f"/v1/fetches/{fetch_id}")
    assert resp.status_code == 404


@then(parsers.parse('the recovery upload for fetch "{fetch_id}" is absent'))
def then_recovery_upload_is_absent(
    acceptance_system: AcceptanceSystem,
    fetch_id: str,
) -> None:
    assert acceptance_system.recovery_upload_absent(fetch_id)


@then(parsers.parse('stderr mentions copy id "{copy_id}"'))
def then_stderr_mentions_copy_id(
    acceptance_context: AcceptanceScenarioContext,
    copy_id: str,
) -> None:
    assert copy_id in _require_command(acceptance_context).stderr


@then(parsers.parse('stderr mentions "{text}"'))
def then_stderr_mentions_text(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    assert text in _require_command(acceptance_context).stderr


@then(parsers.parse('stderr does not mention "{text}"'))
def then_stderr_does_not_mention_text(
    acceptance_context: AcceptanceScenarioContext,
    text: str,
) -> None:
    assert text not in _require_command(acceptance_context).stderr


@then(parsers.parse('stderr does not mention copy id "{copy_id}"'))
def then_stderr_does_not_mention_copy_id(
    acceptance_context: AcceptanceScenarioContext,
    copy_id: str,
) -> None:
    assert copy_id not in _require_command(acceptance_context).stderr


@then("the returned offset matches the previously uploaded bytes")
def then_returned_offset_matches_recorded(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    assert payload["offset"] == acceptance_context.recorded_upload_offset


@then("both upload-session responses contain the same upload url")
def then_upload_session_responses_reuse_upload_url(
    acceptance_context: AcceptanceScenarioContext,
) -> None:
    assert len(acceptance_context.responses) == 2
    payloads = [_json_payload(response) for response in acceptance_context.responses]
    assert payloads[0]["upload_url"] == payloads[1]["upload_url"]


@then(
    parsers.parse(
        'the upload-session length matches fetch "{fetch_id}" entry "{entry_id}" recovery bytes'
    )
)
def then_upload_session_length_matches_manifest_entry_recovery_bytes(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    fetch_id: str,
    entry_id: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    manifest = acceptance_system.fetches.manifest(fetch_id)
    manifest_entry = next(e for e in manifest["entries"] if e["id"] == entry_id)
    assert payload["length"] == manifest_entry["recovery_bytes"]


@then(
    parsers.parse(
        'the upload-session length matches collection "{collection_id}" file "{path}" bytes'
    )
)
def then_upload_session_length_matches_collection_file_bytes(
    acceptance_system: AcceptanceSystem,
    acceptance_context: AcceptanceScenarioContext,
    collection_id: str,
    path: str,
) -> None:
    payload = _json_payload(_require_response(acceptance_context))
    root = acceptance_system.collection_source_root(collection_id)
    assert payload["length"] == len((root / path).read_bytes())
