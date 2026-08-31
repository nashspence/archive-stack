"""Exact public media evidence fixtures shared by maintained target tests."""

from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    SOURCE_ROLE,
    XMP_SOURCE_ROLE,
    MediaProjectionPolicy,
)
from stove0_media_archive_target_support import resolve_media_archive_projection
from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_FACTS_SCHEMA,
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaMetadataFact,
    MediaMetadataFacts,
)
from stove0_observer_protocol import (
    ObservationEvidence,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObservationResultPayload,
    ObserverImplementation,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    CollectionRootRef,
    canonical_json_sha256,
)
from stove0_target_protocol import (
    InputArtifact,
    OperationContract,
    TargetInputAuthority,
    TargetPreflightRequest,
)


def sha(character: str) -> str:
    return character * 64


def media_preflight_request(
    operation: OperationContract,
    intent: dict[str, object],
    *,
    sidecar_capture_time: str = "2025:02:03 04:05:06-0800",
    target_options: dict[str, object] | None = None,
) -> TargetPreflightRequest:
    root = CollectionRootRef(
        collection_id=11,
        archive_root_sha256=sha("1"),
        content_identity=sha("2"),
    )
    inputs = (
        InputArtifact(
            id="primary",
            role=SOURCE_ROLE,
            collection=root,
            path="camera/clip.mov",
            bytes=100,
            sha256=sha("3"),
            media_type="video/quicktime",
        ),
        InputArtifact(
            id="sidecar",
            role=XMP_SOURCE_ROLE,
            collection=root,
            path="camera/clip.xmp",
            bytes=20,
            sha256=sha("4"),
            media_type="application/rdf+xml",
        ),
    )
    subjects = tuple(
        ArtifactSubject(
            id=item.id,
            role=item.role,
            collection=item.collection,
            path=item.path,
            bytes=item.bytes,
            sha256=item.sha256,
            media_type=item.media_type,
        )
        for item in inputs
    )
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=sha("5"),
            observer_registration_id="exiftool",
            observer_descriptor_sha256=sha("6"),
            observer_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
            observer_contract_sha256=MEDIA_METADATA_OBSERVER_CONTRACT.contract_sha256,
            subjects=subjects,
        )
    )
    facts = MediaMetadataFacts(
        artifacts=(
            MediaArtifactFacts(
                artifact_id="primary",
                state="observed",
                facts=(
                    MediaMetadataFact(
                        name="capture-time",
                        value="2025:02:03 04:05:01",
                        evidence=MediaFactEvidence(
                            artifact_id="primary",
                            field="EXIF:DateTimeOriginal",
                        ),
                    ),
                ),
            ),
            MediaArtifactFacts(
                artifact_id="sidecar",
                state="observed",
                facts=(
                    MediaMetadataFact(
                        name="capture-time",
                        value=sidecar_capture_time,
                        evidence=MediaFactEvidence(
                            artifact_id="sidecar",
                            field="XMP-xmp:CreateDate",
                        ),
                    ),
                ),
            ),
        )
    ).model_dump(mode="json")
    result = ObservationResult.seal(
        ObservationResultPayload(
            request_id=request.request_id,
            state="observed",
            observer=ObserverImplementation(
                id="fixture.exiftool/v1",
                version="1.0.0",
                source_revision="fixture",
                descriptor_sha256=sha("6"),
            ),
            observer_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
            observer_contract_sha256=MEDIA_METADATA_OBSERVER_CONTRACT.contract_sha256,
            subjects=subjects,
            facts_schema=MEDIA_METADATA_FACTS_SCHEMA,
            facts=facts,
            facts_sha256=canonical_json_sha256(facts),
        )
    )
    archive_directory, archive_suffix = (
        ("audio", ".opus") if operation.id == AUDIO_ARCHIVE_OPERATION.id else ("video", ".mkv")
    )
    projection = resolve_media_archive_projection(
        inputs=inputs,
        observations=(ObservationEvidence(request=request, result=result),),
        policy=MediaProjectionPolicy.model_validate(intent["metadata_projection"]),
        archive_directory=archive_directory,
        archive_suffix=archive_suffix,
    )
    return TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        inputs=TargetInputAuthority.from_selection(ArtifactSelection.seal(subjects)),
        intent=intent,
        target_options={
            **(target_options or {}),
            "media_projection": projection.model_dump(mode="json"),
        },
        observations=(ObservationEvidence(request=request, result=result),),
    )


__all__ = ["media_preflight_request", "sha"]
