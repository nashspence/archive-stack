from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import get_args

from stove0_media_archive_target_contracts import (
    AUDIO_ARCHIVE_OPERATION,
    AV1_OPUS_ARCHIVE_OPERATION,
    METADATA_XMP_ROLE,
    SOURCE_ARTIFACT_ROLE,
    SOURCE_ROLE,
    XMP_SOURCE_ROLE,
    Av1OpusArchiveIntent,
    MediaFieldPreference,
    MediaGps,
    MediaProjectionFieldName,
    MediaProjectionPolicy,
)
from stove0_media_archive_target_support import (
    MEDIA_FACT_PROJECTION_FIELDS,
    ffmpeg_container_metadata_args,
    render_projection_xmp,
    resolve_media_archive_projection,
)
from stove0_media_metadata_observer_contracts import (
    MEDIA_METADATA_FACTS_SCHEMA,
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaArtifactFacts,
    MediaFactEvidence,
    MediaFactName,
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
    ArtifactSubject,
    CollectionRootRef,
    canonical_json_sha256,
)
from stove0_target_protocol import InputArtifact


def _sha(character: str) -> str:
    return character * 64


def _root() -> CollectionRootRef:
    return CollectionRootRef(
        collection_id=1,
        archive_root_sha256=_sha("1"),
        content_identity=_sha("2"),
    )


def _evidence(
    inputs: tuple[InputArtifact, ...],
    facts: dict[str, tuple[MediaMetadataFact, ...]],
) -> tuple[ObservationEvidence, ...]:
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
            work_id=_sha("3"),
            observer_registration_id="exiftool",
            observer_descriptor_sha256=_sha("4"),
            observer_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
            observer_contract_sha256=MEDIA_METADATA_OBSERVER_CONTRACT.contract_sha256,
            subjects=subjects,
        )
    )
    document = MediaMetadataFacts(
        artifacts=tuple(
            MediaArtifactFacts(
                artifact_id=item.id,
                state="observed" if facts.get(item.id) else "unsupported",
                facts=facts.get(item.id, ()),
            )
            for item in inputs
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
                descriptor_sha256=_sha("4"),
            ),
            observer_contract_id=MEDIA_METADATA_OBSERVER_CONTRACT.id,
            observer_contract_sha256=MEDIA_METADATA_OBSERVER_CONTRACT.contract_sha256,
            subjects=subjects,
            facts_schema=MEDIA_METADATA_FACTS_SCHEMA,
            facts=document,
            facts_sha256=canonical_json_sha256(document),
        )
    )
    return (ObservationEvidence(request=request, result=result),)


def test_audio_only_archive_cannot_retire_its_richer_source_collection() -> None:
    assert AUDIO_ARCHIVE_OPERATION.source_retirement_permitted is False


def test_projection_bridge_exhaustively_maps_independent_contract_vocabularies() -> None:
    assert set(MEDIA_FACT_PROJECTION_FIELDS) == set(get_args(MediaFactName))
    projected = {field for field in MEDIA_FACT_PROJECTION_FIELDS.values() if field is not None}
    assert projected == set(get_args(MediaProjectionFieldName))
    assert MEDIA_FACT_PROJECTION_FIELDS["container-format"] is None


def test_archive_video_retirement_always_requires_reconstructive_source_artifacts() -> None:
    source_artifacts = next(
        output
        for output in AV1_OPUS_ARCHIVE_OPERATION.outputs
        if output.role == SOURCE_ARTIFACT_ROLE
    )

    assert AV1_OPUS_ARCHIVE_OPERATION.source_retirement_permitted
    assert source_artifacts.minimum == 1
    assert "preserve_source_artifacts" not in Av1OpusArchiveIntent.model_json_schema()["properties"]


def test_media_observation_contract_binds_each_fact_to_its_exact_artifact() -> None:
    facts = MediaMetadataFacts(
        artifacts=(
            MediaArtifactFacts(
                artifact_id="primary",
                state="observed",
                facts=(
                    MediaMetadataFact(
                        name="capture-time",
                        value="2025:02:03 04:05:06-08:00",
                        evidence=MediaFactEvidence(
                            artifact_id="primary",
                            field="XMP-xmp:CreateDate",
                        ),
                    ),
                ),
            ),
        )
    )

    schema = MEDIA_METADATA_OBSERVER_CONTRACT.facts_schema.document
    assert facts.model_dump(mode="json")["artifacts"][0]["artifact_id"] == "primary"
    assert schema["properties"]["artifacts"].get("maxItems") is None


def test_media_projection_accepts_large_collection_and_assertion_sets() -> None:
    inputs = tuple(
        InputArtifact(
            id=f"primary-{index:03d}",
            role=SOURCE_ROLE,
            collection=_root(),
            path=f"camera/clip-{index:03d}.mov",
            bytes=index,
            sha256=f"{index % 16:x}" * 64,
            media_type="video/quicktime",
        )
        for index in range(257)
    )
    observations = _evidence(inputs, {})

    projection = resolve_media_archive_projection(
        inputs=inputs,
        observations=observations,
        policy=MediaProjectionPolicy(),
        archive_directory="video",
        archive_suffix=".mkv",
    )

    assert len(projection.items) == 257
    assert len(projection.observation_result_sha256s) == 1
    projection.validate_plan_evidence((*projection.observation_result_sha256s, _sha("e")))

    try:
        projection.validate_plan_evidence((_sha("e"),))
    except ValueError as error:
        assert str(error) == "media projection evidence is absent from the target plan"
    else:
        raise AssertionError("projection accepted a plan that omitted its exact evidence")


def test_projection_retains_conflicts_and_only_selects_explicit_evidence() -> None:
    root = _root()
    inputs = (
        InputArtifact(
            id="primary",
            role=SOURCE_ROLE,
            collection=root,
            path="camera/clip.mov",
            bytes=100,
            sha256=_sha("5"),
            media_type="video/quicktime",
        ),
        InputArtifact(
            id="sidecar",
            role=XMP_SOURCE_ROLE,
            collection=root,
            path="camera/clip.xmp",
            bytes=20,
            sha256=_sha("6"),
            media_type="application/rdf+xml",
        ),
    )
    facts = {
        "primary": (
            MediaMetadataFact(
                name="capture-time",
                value="2025:02:03 04:05:01",
                evidence=MediaFactEvidence(
                    artifact_id="primary",
                    field="EXIF:DateTimeOriginal",
                ),
            ),
        ),
        "sidecar": (
            MediaMetadataFact(
                name="capture-time",
                value="2025:02:03 04:05:06-0800",
                evidence=MediaFactEvidence(
                    artifact_id="sidecar",
                    field="XMP-xmp:CreateDate",
                ),
            ),
        ),
    }
    observations = _evidence(inputs, facts)
    unresolved = resolve_media_archive_projection(
        inputs=inputs,
        observations=observations,
        policy=MediaProjectionPolicy(),
        archive_directory="video",
        archive_suffix=".mkv",
    )
    item = unresolved.items[0]

    assert item.associated_sidecar_artifact_ids == ("sidecar",)
    assert [fact.value for fact in item.assertions] == [
        "2025:02:03 04:05:01",
        "2025:02:03 04:05:06-0800",
    ]
    assert not any(value.name == "capture-time" for value in item.selected)
    xmp = render_projection_xmp(item).decode()
    assert "2025:02:03 04:05:01" in xmp
    assert "2025:02:03 04:05:06-0800" in xmp
    assert "xmp:CreateDate=" not in xmp

    resolved = resolve_media_archive_projection(
        inputs=inputs,
        observations=observations,
        policy=MediaProjectionPolicy(
            device_make="Example Camera Corp",
            device_model="Example Camera One",
            gps=MediaGps(latitude=45.5, longitude=-122.6),
            creators=("Alex Example", "River Example"),
            tags=("archive/example",),
            field_preferences=(
                MediaFieldPreference(
                    name="capture-time",
                    fields=("XMP-xmp:CreateDate", "EXIF:DateTimeOriginal"),
                ),
            ),
        ),
        archive_directory="video",
        archive_suffix=".mkv",
    )
    selected = {value.name: value.value for value in resolved.items[0].selected}

    assert selected["capture-time"] == "2025-02-03T04:05:06-08:00"
    assert selected["creator"] == ["Alex Example", "River Example"]
    assert resolved.projection_sha256 != unresolved.projection_sha256
    assert resolved.retained_xmp_sidecars[0].input_artifact_id == "sidecar"
    args = ffmpeg_container_metadata_args(resolved.items[0])
    assert "creation_time=2025-02-03T04:05:06-08:00" in args
    assert "ARTIST=Alex Example; River Example" in args
    assert "LOCATION=+45.50000000-122.60000000/" in args
    rendered = render_projection_xmp(
        resolved.items[0],
        tags=("archive/example",),
    )
    assert b'xmp:CreateDate="2025-02-03T04:05:06-08:00"' in rendered
    assert b"Alex Example" in rendered
    ET.fromstring(rendered)
    assert {output.role for output in AV1_OPUS_ARCHIVE_OPERATION.outputs} >= {
        METADATA_XMP_ROLE,
        SOURCE_ARTIFACT_ROLE,
    }
