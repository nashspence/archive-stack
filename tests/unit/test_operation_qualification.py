from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/operation_qualification.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("operation_qualification", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_operation_matrix_is_complete_and_fail_closed() -> None:
    module = load_script()

    matrix = module.operation_matrix()

    identities = {(item.application, item.operation_id, item.method, item.path) for item in matrix}
    assert len(identities) == len(matrix)
    assert {item.application for item in matrix} == {"riverhog", "munchy", "jeb"}
    assert {item.classification for item in matrix} == {
        "human-cli+json",
        "client-only-primitive",
        "standard-tool/protocol",
        "service-internal",
    }
    assert all(
        item.client is not None
        for item in matrix
        if item.classification in {"human-cli+json", "client-only-primitive"}
    )
    assert all(item.cli_commands for item in matrix if item.classification == "human-cli+json")


def test_operation_audiences_distinguish_commands_wires_and_protocols() -> None:
    module = load_script()
    by_identity = {
        (item.application, item.operation_id): item for item in module.operation_matrix()
    }

    assert by_identity[("riverhog", "list_collections")].classification == "human-cli+json"
    assert "collection list" in by_identity[("riverhog", "list_collections")].cli_commands
    assert (
        by_identity[("riverhog", "put_collection_upload_session_unit")].classification
        == "client-only-primitive"
    )
    assert (
        by_identity[("riverhog", "get_portable_collection_manifest")].classification
        == "standard-tool/protocol"
    )
    assert (
        by_identity[("riverhog", "head_retrieval_file")].classification == "standard-tool/protocol"
    )
    assert (
        by_identity[("munchy", "create_or_resume_submission_file_upload")].classification
        == "client-only-primitive"
    )
    assert by_identity[("munchy", "list_jobs")].classification == "human-cli+json"
    assert (
        by_identity[("munchy", "patch_tusd_submission_file")].classification
        == "standard-tool/protocol"
    )
    assert by_identity[("munchy", "patch_tusd_submission_file")].client == "MunchyClient"
    assert by_identity[("jeb", "handle_tus_hook")].classification == "service-internal"
    assert (
        by_identity[("jeb", "create_tusd_ingress_upload")].classification
        == "standard-tool/protocol"
    )
    assert (
        by_identity[("jeb", "put_public_tus_ingress_provenance_binding")].classification
        == "client-only-primitive"
    )
    assert by_identity[("jeb", "put_public_tus_ingress_provenance_binding")].client == (
        "JebIngressClient"
    )
    assert by_identity[("munchy", "authorize_tusd_upload")].classification == "service-internal"


def test_exact_sha_evidence_contains_only_generated_current_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    module = load_script()
    source_sha = "a" * 40
    monkeypatch.setattr(module, "_git_head", lambda: source_sha)
    monkeypatch.setattr(
        module,
        "_cold_cli_timings",
        lambda: {"riverhog": {"trials": 3, "median_ms": 1.0}},
    )

    matrix = module.operation_matrix()
    timings = tmp_path / "timings.json"
    timing_rows = []
    for item in matrix:
        if item.provider_evidence is not None:
            continue
        row = {
            "application": item.application,
            "operation_id": item.operation_id,
            "server_wall": {
                "samples": 1,
                "minimum_ms": 1.0,
                "median_ms": 1.0,
                "maximum_ms": 1.0,
            },
        }
        if item.classification in {"human-cli+json", "client-only-primitive"}:
            row["client_wall"] = {
                "samples": 1,
                "minimum_ms": 2.0,
                "median_ms": 2.0,
                "maximum_ms": 2.0,
            }
        timing_rows.append(row)
    timings.write_text(
        json.dumps(
            {
                "schema": "riverhog-operation-timings/v1",
                "source_sha": source_sha,
                "pytest_exit_status": 0,
                "operations": timing_rows,
            }
        )
    )

    payload = module.evidence(source_sha=source_sha, timings=timings)
    operations = payload["operations"]

    assert payload["schema"] == "riverhog-operation-qualification/v1"
    assert payload["source_sha"] == source_sha
    assert payload["summary"]["operations"] == len(operations)
    assert payload["summary"]["matrix_sha256"] == module._matrix_sha256(module.operation_matrix())
    assert payload["provider_evidence"]["issue"] == 442
    assert payload["provider_evidence"]["required_for"]
    assert payload["performance"]["cold_cli_startup"]["riverhog"]["median_ms"] == 1.0
    assert payload["performance"]["local_api"]["operations"]
    assert payload["qualification"]["positive_local_lifecycles"]["status"] == "passed"
    assert payload["qualification"]["cli_human_json_projection"]["status"] == "passed"
    assert payload["qualification"]["bounded_state_access"]["status"] == "passed"
    assert payload["qualification"]["event_cursor_restart_resume"]["status"] == "passed"
    assert all(set(item) == set(module.Operation.__dataclass_fields__) for item in operations)


def test_timing_evidence_fails_closed_on_missing_local_operation(tmp_path: Path) -> None:
    module = load_script()
    source_sha = "b" * 40
    timings = tmp_path / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "schema": "riverhog-operation-timings/v1",
                "source_sha": source_sha,
                "pytest_exit_status": 0,
                "operations": [],
            }
        )
    )

    try:
        module._load_operation_timings(
            timings,
            source_sha=source_sha,
            matrix=module.operation_matrix(),
        )
    except module.QualificationError as exc:
        assert "lack positive local timing witnesses" in str(exc)
    else:
        raise AssertionError("missing local operation witnesses must fail qualification")
