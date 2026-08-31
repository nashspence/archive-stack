from __future__ import annotations

import pytest
from pydantic import ValidationError
from riverhog_protocol import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyObjectDocument,
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadUnitAssignmentDocument,
    CollectionUploadWorkBatchDocument,
    validate_collection_upload_artifact_custody_receipt,
    validate_collection_upload_batch_against_registration_constraints,
)
from riverhog_protocol.collection_workflows import DERIVATION_EVIDENCE_PATH
from riverhog_protocol.manifest import collection_content_identity
from riverhog_protocol.raw_ingress import ordered_raw_part_commitment


def test_server_upload_creation_identity_is_not_public_protocol() -> None:
    import riverhog_protocol

    assert "CollectionUploadCreationIdentityDocument" not in riverhog_protocol.__all__
    assert "CollectionUploadCreationIdentityPayload" not in riverhog_protocol.__all__


def _file(path: str) -> dict[str, object]:
    return {
        "path": path,
        "bytes": 1,
        "sha256": "a" * 64,
        "provenance": {
            "status": "omitted",
            "omission_reason": "source did not expose provenance",
        },
    }


def _constraints(*, pack_member_bytes: int = 1024, raw_part_bytes: int = 65536) -> dict[str, int]:
    return {
        "pack_member_bytes": pack_member_bytes,
        "raw_part_plaintext_bytes": raw_part_bytes,
    }


def _raw_parts(*sha256s: str, part_plaintext_bytes: int = 65536) -> dict[str, object]:
    count, commitment = ordered_raw_part_commitment(sha256s)
    return {
        "part_plaintext_bytes": part_plaintext_bytes,
        "part_count": count,
        "ordered_sha256": commitment,
    }


def test_direct_ingress_file_contract_is_canonical_and_reusable() -> None:
    file = CollectionUploadFileIn.model_validate(_file("camera/clip.mp4"))
    batch = CollectionUploadFileBatchDocument(files=[file])

    assert batch.model_dump(mode="json") == {
        "files": [{**_file("camera/clip.mp4"), "raw_parts": None}]
    }
    schema = CollectionUploadFileIn.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["path"] == {"$ref": "#/$defs/CanonicalRelPath"}
    assert schema["$defs"]["CanonicalRelPath"]["pattern"] == r"^[^/\\]+(?:/[^/\\]+)*$"


@pytest.mark.parametrize("path", (" camera/clip.mp4", "camera//clip.mp4", "camera/../clip.mp4"))
def test_direct_ingress_rejects_noncanonical_file_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        CollectionUploadFileIn.model_validate(_file(path))


def test_direct_ingress_batch_preserves_the_server_order_contract() -> None:
    with pytest.raises(ValidationError, match="canonical path order"):
        CollectionUploadFileBatchDocument.model_validate(
            {"files": [_file("z.txt"), _file("a.txt")]}
        )


def test_terminal_derivation_evidence_is_the_only_nonlexical_upload_member() -> None:
    files = [
        _file("riverhog/producer-evidence.json"),
        _file(DERIVATION_EVIDENCE_PATH),
        _file("video/source/archive.mkv"),
    ]
    batch = CollectionUploadFileBatchDocument.model_validate(
        {"files": [files[0], files[2], files[1]]}
    )

    assert [item.path for item in batch.files] == [
        "riverhog/producer-evidence.json",
        "video/source/archive.mkv",
        DERIVATION_EVIDENCE_PATH,
    ]
    identity = collection_content_identity(
        (str(item["path"]), int(item["bytes"]), str(item["sha256"])) for item in files
    )
    reordered = collection_content_identity(
        (str(item["path"]), int(item["bytes"]), str(item["sha256"])) for item in reversed(files)
    )
    assert identity == reordered


def test_artifact_custody_receipt_seals_exact_recovering_objects() -> None:
    receipt = CollectionUploadArtifactCustodyReceiptDocument.seal(
        collection_id=42,
        path="video/source/archive.mkv",
        bytes=123,
        sha256="a" * 64,
        archive_objects=(
            CollectionUploadCustodyObjectDocument(
                volume_id="segment-" + "0" * 63 + "1",
                sealed_receipt_sha256="b" * 64,
            ),
            CollectionUploadCustodyObjectDocument(
                volume_id="segment-" + "0" * 63 + "2",
                sealed_receipt_sha256="c" * 64,
            ),
        ),
    )

    assert receipt.path == "video/source/archive.mkv"
    assert receipt.archive_object_count == 2
    assert len(receipt.archive_object_set_sha256) == 64
    assert (
        CollectionUploadArtifactCustodyReceiptDocument.model_validate_json(
            receipt.model_dump_json()
        )
        == receipt
    )
    assert (
        validate_collection_upload_artifact_custody_receipt(
            42,
            CollectionUploadFileIn.model_validate(
                {
                    **_file("video/source/archive.mkv"),
                    "bytes": 123,
                }
            ),
            receipt,
        )
        is receipt
    )
    artifact = CollectionUploadFileIn.model_validate(
        {
            **_file("video/source/archive.mkv"),
            "bytes": 123,
        }
    )
    mismatches = (
        (43, artifact),
        (42, artifact.model_copy(update={"path": "video/source/other.mkv"})),
        (42, artifact.model_copy(update={"bytes": 124})),
        (42, artifact.model_copy(update={"sha256": "c" * 64})),
    )
    for collection_id, changed_artifact in mismatches:
        with pytest.raises(ValueError, match="upload file identity"):
            validate_collection_upload_artifact_custody_receipt(
                collection_id,
                changed_artifact,
                receipt,
            )


def test_direct_ingress_batch_rejects_duplicate_file_paths() -> None:
    with pytest.raises(ValidationError, match="paths must be unique"):
        CollectionUploadFileBatchDocument.model_validate(
            {"files": [_file("a.txt"), _file("a.txt")]}
        )


def test_direct_ingress_registration_constraints_bind_raw_part_declarations() -> None:
    raw = {
        **_file("video.bin"),
        "bytes": 65537,
        "raw_parts": _raw_parts("b" * 64, "c" * 64),
    }
    batch = CollectionUploadFileBatchDocument.model_validate({"files": [raw]})
    constraints = CollectionUploadRegistrationConstraintsDocument.model_validate(
        _constraints(pack_member_bytes=1)
    )

    assert (
        validate_collection_upload_batch_against_registration_constraints(batch, constraints)
        is batch
    )


def test_direct_ingress_registration_constraints_expose_only_producer_policy() -> None:
    schema = CollectionUploadRegistrationConstraintsDocument.model_json_schema()

    assert set(schema["properties"]) == {
        "pack_member_bytes",
        "raw_part_plaintext_bytes",
    }
    assert schema["additionalProperties"] is False


def test_server_planned_upload_work_uses_protocol_owned_exact_identities() -> None:
    assignment = CollectionUploadUnitAssignmentDocument(
        volume={
            "volume_id": "pack-" + "0" * 63 + "3",
            "sequence": 3,
            "kind": "pack",
        },
        plan_sha256="b" * 64,
        unit={
            "unit": 0,
            "payload_bytes": 5,
            "plaintext_bytes": 2048,
            "sources": [
                {
                    "path": "camera/clip.mp4",
                    "offset": 0,
                    "bytes": 5,
                    "artifact_sha256": "a" * 64,
                }
            ],
            "state": "pending",
        },
    )

    assert (
        CollectionUploadUnitAssignmentDocument.model_validate_json(assignment.model_dump_json())
        == assignment
    )
    assert assignment.unit.sources[0].path == "camera/clip.mp4"

    changed = assignment.model_dump(mode="python")
    changed["unit"]["payload_bytes"] = 4
    with pytest.raises(ValidationError, match="source bytes"):
        CollectionUploadUnitAssignmentDocument.model_validate(changed)


def test_bounded_upload_work_batch_binds_assignment_and_checkpoint_state() -> None:
    assignment = CollectionUploadUnitAssignmentDocument.model_validate(
        {
            "volume": {
                "volume_id": "pack-" + "0" * 64,
                "sequence": 0,
                "kind": "pack",
            },
            "plan_sha256": "b" * 64,
            "unit": {
                "unit": 0,
                "payload_bytes": 5,
                "plaintext_bytes": 5,
                "sources": [
                    {
                        "path": "camera/clip.mp4",
                        "offset": 0,
                        "bytes": 5,
                        "artifact_sha256": "a" * 64,
                    }
                ],
                "state": "pending",
            },
        }
    )
    batch = CollectionUploadWorkBatchDocument(
        collection_id=7,
        planning_complete=False,
        complete=False,
        committed_payload_bytes=0,
        work=[assignment],
    )

    assert batch.work == [assignment]
    repeated = batch.model_dump(mode="python")
    repeated["work"] = [repeated["work"][0], repeated["work"][0]]
    with pytest.raises(ValidationError, match="unique"):
        CollectionUploadWorkBatchDocument.model_validate(repeated)
    completed = batch.model_dump(mode="python")
    completed["complete"] = True
    with pytest.raises(ValidationError, match="completion"):
        CollectionUploadWorkBatchDocument.model_validate(completed)


@pytest.mark.parametrize(
    "volume_id",
    ("raw-000000000000", "pack-0", "pack-00000000000A", " pack-000000000000"),
)
def test_upload_volume_identity_rejects_noncanonical_forms(volume_id: str) -> None:
    with pytest.raises(ValidationError):
        CollectionUploadCustodyObjectDocument(
            volume_id=volume_id,
            sealed_receipt_sha256="b" * 64,
        )


@pytest.mark.parametrize("raw_part_bytes", (1, 65535, 65537, 131071))
def test_direct_ingress_rejects_unsatisfiable_raw_part_constraints(
    raw_part_bytes: int,
) -> None:
    with pytest.raises(ValidationError):
        CollectionUploadRegistrationConstraintsDocument.model_validate(
            _constraints(raw_part_bytes=raw_part_bytes)
        )


@pytest.mark.parametrize(
    "file_payload",
    (
        {**_file("small.bin"), "raw_parts": _raw_parts("b" * 64)},
        {**_file("large.bin"), "bytes": 1024},
        {
            **_file("large.bin"),
            "bytes": 1024,
            "raw_parts": _raw_parts("b" * 64, part_plaintext_bytes=131072),
        },
        {
            **_file("large.bin"),
            "bytes": 65537,
            "raw_parts": _raw_parts("b" * 64),
        },
    ),
)
def test_direct_ingress_constraints_reject_inconsistent_raw_parts(
    file_payload: dict[str, object],
) -> None:
    batch = CollectionUploadFileBatchDocument.model_validate({"files": [file_payload]})
    constraints = CollectionUploadRegistrationConstraintsDocument.model_validate(_constraints())

    with pytest.raises(ValueError):
        validate_collection_upload_batch_against_registration_constraints(batch, constraints)


def test_direct_ingress_captured_provenance_uses_canonical_reference_ids() -> None:
    payload = {
        **_file("camera/clip.mp4"),
        "provenance": {
            "status": "captured",
            "journal_id": "journal-1",
            "current_state_id": "state-1",
        },
    }

    with pytest.raises(ValidationError):
        CollectionUploadFileIn.model_validate(payload)
