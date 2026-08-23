"""Pure, target-independent media projection planning and rendering."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from html import escape
from pathlib import PurePosixPath
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from riverhog_protocol.paths import normalize_relpath
from stove0_observer_protocol import canonical_json_bytes, canonical_json_sha256
from stove0_protocol import ArtifactSubject, ObservationEvidence
from stove0_target_protocol import InputArtifact

from stove0_media_archive_contracts.observation import (
    MEDIA_METADATA_OBSERVATION_ID,
    MediaFactEvidence,
    MediaFactName,
    MediaMetadataFact,
    MediaMetadataFacts,
)
from stove0_media_archive_contracts.roles import SOURCE_ROLE, XMP_SOURCE_ROLE

MEDIA_PROJECTION_FORMAT: Literal["stove0-media-archive-projection/v1"] = (
    "stove0-media-archive-projection/v1"
)


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaGps(ProjectionModel):
    latitude: float
    longitude: float

    @model_validator(mode="after")
    def valid_position(self) -> Self:
        if not (
            math.isfinite(self.latitude)
            and math.isfinite(self.longitude)
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
        ):
            raise ValueError("media projection GPS coordinates are invalid")
        return self


class MediaFieldPreference(ProjectionModel):
    name: MediaFactName
    fields: tuple[str, ...] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("media projection evidence fields must be canonical")
        if len(value) != len(set(value)):
            raise ValueError("media projection evidence fields must be unique")
        return value


class MediaProjectionPolicy(ProjectionModel):
    """Portable recipe-owned choices; omitted values are never manufactured."""

    format: Literal["stove0-media-projection-policy/v1"] = "stove0-media-projection-policy/v1"
    device_make: str | None = None
    device_model: str | None = None
    gps: MediaGps | None = None
    creators: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    field_preferences: tuple[MediaFieldPreference, ...] = ()

    @field_validator("device_make", "device_model")
    @classmethod
    def canonical_optional_text(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("configured media metadata must be canonical")
        return value

    @field_validator("creators")
    @classmethod
    def canonical_creators(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("configured media creators must be canonical")
        if len(value) != len(set(value)):
            raise ValueError("configured media creators must be unique")
        return value

    @field_validator("tags")
    @classmethod
    def canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("configured media tags must be canonical")
        if value != tuple(sorted(set(value))):
            raise ValueError("configured media tags must be unique and ordered")
        return value

    @field_validator("field_preferences")
    @classmethod
    def canonical_preferences(
        cls,
        value: tuple[MediaFieldPreference, ...],
    ) -> tuple[MediaFieldPreference, ...]:
        names = [item.name for item in value]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("media field preferences must be unique and ordered by fact name")
        return value


class MediaProjectedValue(ProjectionModel):
    name: MediaFactName
    value: JsonValue
    source: Literal["observation", "recipe"]
    evidence: tuple[MediaFactEvidence, ...] = ()

    @field_validator("evidence")
    @classmethod
    def canonical_evidence(
        cls,
        value: tuple[MediaFactEvidence, ...],
    ) -> tuple[MediaFactEvidence, ...]:
        keys = [(item.artifact_id, item.field) for item in value]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("projected value evidence must be unique and ordered")
        return value

    @model_validator(mode="after")
    def bind_source(self) -> Self:
        if (self.source == "observation") != bool(self.evidence):
            raise ValueError("projected observation values require exact evidence")
        return self


class MediaProjectionItem(ProjectionModel):
    input_artifact_id: str
    associated_sidecar_artifact_ids: tuple[str, ...] = ()
    archive_path: str
    xmp_path: str
    assertions: tuple[MediaMetadataFact, ...] = ()
    selected: tuple[MediaProjectedValue, ...] = ()

    @field_validator("associated_sidecar_artifact_ids")
    @classmethod
    def canonical_sidecars(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("associated XMP artifact IDs must be unique and ordered")
        return value

    @field_validator("archive_path", "xmp_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        if normalize_relpath(value) != value:
            raise ValueError("media projection output paths must be canonical")
        return value

    @model_validator(mode="after")
    def canonical_evidence(self) -> Self:
        assertion_keys = [_fact_key(item) for item in self.assertions]
        if assertion_keys != sorted(assertion_keys) or len(assertion_keys) != len(
            set(assertion_keys)
        ):
            raise ValueError("media projection assertions must be unique and ordered")
        names = [item.name for item in self.selected]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("projected media values must be unique and ordered by fact name")
        sources = {self.input_artifact_id, *self.associated_sidecar_artifact_ids}
        if any(item.evidence.artifact_id not in sources for item in self.assertions):
            raise ValueError("media projection assertion is outside its exact input group")
        assertion_evidence = {item.evidence for item in self.assertions}
        if any(
            evidence not in assertion_evidence
            for value in self.selected
            for evidence in value.evidence
        ):
            raise ValueError("projected value evidence has no retained assertion")
        return self

    @property
    def derived_from(self) -> tuple[str, ...]:
        return tuple(sorted((self.input_artifact_id, *self.associated_sidecar_artifact_ids)))


class RetainedXmpSidecar(ProjectionModel):
    input_artifact_id: str
    output_path: str

    @field_validator("output_path")
    @classmethod
    def canonical_path(cls, value: str) -> str:
        if normalize_relpath(value) != value:
            raise ValueError("retained XMP output path must be canonical")
        return value


class MediaArchiveProjectionPayload(ProjectionModel):
    format: Literal["stove0-media-archive-projection/v1"] = MEDIA_PROJECTION_FORMAT
    observation_result_sha256s: tuple[str, ...]
    items: tuple[MediaProjectionItem, ...] = Field(min_length=1)
    retained_xmp_sidecars: tuple[RetainedXmpSidecar, ...] = ()

    @model_validator(mode="after")
    def canonical_members(self) -> Self:
        if self.observation_result_sha256s != tuple(sorted(set(self.observation_result_sha256s))):
            raise ValueError("projection observation results must be unique and ordered")
        item_ids = [item.input_artifact_id for item in self.items]
        if item_ids != sorted(item_ids) or len(item_ids) != len(set(item_ids)):
            raise ValueError("media projection items must be unique and ordered")
        sidecar_ids = [item.input_artifact_id for item in self.retained_xmp_sidecars]
        if sidecar_ids != sorted(sidecar_ids) or len(sidecar_ids) != len(set(sidecar_ids)):
            raise ValueError("retained XMP sidecars must be unique and ordered")
        output_paths = [
            *(item.archive_path for item in self.items),
            *(item.xmp_path for item in self.items),
            *(item.output_path for item in self.retained_xmp_sidecars),
        ]
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("media projection output paths must be unique")
        return self


class MediaArchiveProjection(MediaArchiveProjectionPayload):
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        document = self.model_dump(
            mode="json",
            exclude={"projection_sha256"},
            exclude_none=True,
        )
        if canonical_json_sha256(document) != self.projection_sha256:
            raise ValueError("media projection digest does not match its canonical payload")
        return self

    @classmethod
    def seal(cls, payload: MediaArchiveProjectionPayload) -> MediaArchiveProjection:
        document = payload.model_dump(mode="json", exclude_none=True)
        return cls(**document, projection_sha256=canonical_json_sha256(document))

    def item_for(self, artifact_id: str) -> MediaProjectionItem:
        for item in self.items:
            if item.input_artifact_id == artifact_id:
                return item
        raise KeyError(artifact_id)

    def validate_plan_evidence(self, observation_result_sha256s: Sequence[str]) -> None:
        """Require every projection source while permitting unrelated observers."""

        available = set(observation_result_sha256s)
        if any(item not in available for item in self.observation_result_sha256s):
            raise ValueError("media projection evidence is absent from the target plan")


def resolve_media_archive_projection(
    *,
    inputs: Sequence[InputArtifact],
    observations: Sequence[ObservationEvidence],
    policy: MediaProjectionPolicy,
    archive_directory: str,
    archive_suffix: str,
) -> MediaArchiveProjection:
    """Resolve exact media output semantics without reading any payload bytes."""

    if not archive_suffix.startswith(".") or "/" in archive_suffix:
        raise ValueError("media archive suffix must be a simple extension")
    primary = tuple(item for item in inputs if item.role == SOURCE_ROLE)
    sidecars = tuple(item for item in inputs if item.role == XMP_SOURCE_ROLE)
    unknown = sorted({item.role for item in inputs} - {SOURCE_ROLE, XMP_SOURCE_ROLE})
    if unknown:
        raise ValueError("media projection received unsupported input roles: " + ", ".join(unknown))
    if not primary:
        raise ValueError("media projection requires at least one primary artifact")

    relevant = tuple(
        item
        for item in observations
        if item.request.observer_contract_id == MEDIA_METADATA_OBSERVATION_ID
    )
    if not relevant:
        raise ValueError("media projection requires exact media observation evidence")
    facts_by_artifact: dict[str, tuple[MediaMetadataFact, ...]] = {}
    subjects_by_artifact: dict[str, ArtifactSubject] = {}
    for evidence in relevant:
        facts = MediaMetadataFacts.model_validate(evidence.result.facts)
        fact_ids = {item.artifact_id for item in facts.artifacts}
        subject_ids = {item.id for item in evidence.request.subjects}
        if fact_ids != subject_ids:
            raise ValueError("media observation facts differ from their exact subjects")
        for subject in evidence.request.subjects:
            if subject.id in subjects_by_artifact:
                raise ValueError("media observation evidence repeats an artifact subject")
            subjects_by_artifact[subject.id] = subject
        for fact_group in facts.artifacts:
            if fact_group.artifact_id in facts_by_artifact:
                raise ValueError("media observation evidence repeats an artifact")
            facts_by_artifact[fact_group.artifact_id] = fact_group.facts
    missing = sorted(item.id for item in inputs if item.id not in facts_by_artifact)
    if missing:
        raise ValueError("media observation evidence omits exact inputs: " + ", ".join(missing))
    for item in inputs:
        subject = subjects_by_artifact[item.id]
        expected = {
            "id": item.id,
            "role": item.role,
            "collection": item.collection,
            "path": item.path,
            "bytes": item.bytes,
            "sha256": item.sha256,
            "media_type": item.media_type,
        }
        if any(getattr(subject, key) != value for key, value in expected.items()):
            raise ValueError("media observation evidence differs from an exact target input")

    sidecars_by_stem: dict[tuple[str, str], list[InputArtifact]] = {}
    for sidecar in sidecars:
        path = PurePosixPath(sidecar.path)
        sidecars_by_stem.setdefault((str(path.parent), path.stem), []).append(sidecar)

    items: list[MediaProjectionItem] = []
    for primary_artifact in primary:
        path = PurePosixPath(primary_artifact.path)
        associated = tuple(
            sorted(
                sidecars_by_stem.get((str(path.parent), path.stem), ()), key=lambda item: item.id
            )
        )
        assertions = tuple(
            sorted(
                (
                    *facts_by_artifact[primary_artifact.id],
                    *(fact for sidecar in associated for fact in facts_by_artifact[sidecar.id]),
                ),
                key=_fact_key,
            )
        )
        items.append(
            MediaProjectionItem(
                input_artifact_id=primary_artifact.id,
                associated_sidecar_artifact_ids=tuple(item.id for item in associated),
                archive_path=f"{archive_directory}/{primary_artifact.id}{archive_suffix}",
                xmp_path=f"{archive_directory}/{primary_artifact.id}{archive_suffix}.xmp",
                assertions=assertions,
                selected=_select_values(assertions, policy),
            )
        )
    retained = tuple(
        RetainedXmpSidecar(
            input_artifact_id=item.id,
            output_path=f"source-artifacts/{item.id}.xmp",
        )
        for item in sorted(sidecars, key=lambda item: item.id)
    )
    return MediaArchiveProjection.seal(
        MediaArchiveProjectionPayload(
            observation_result_sha256s=tuple(
                sorted(item.result.result_sha256 for item in relevant)
            ),
            items=tuple(sorted(items, key=lambda item: item.input_artifact_id)),
            retained_xmp_sidecars=retained,
        )
    )


def render_projection_xmp(
    item: MediaProjectionItem,
    *,
    tags: Sequence[str] = (),
) -> bytes:
    """Render deterministic XMP with standard selected values and all assertions."""

    selected = {value.name: value.value for value in item.selected}
    attributes: dict[str, str] = {}
    capture_time = selected.get("capture-time")
    if isinstance(capture_time, str):
        attributes.update(
            {
                "exif:DateTimeOriginal": capture_time,
                "photoshop:DateCreated": capture_time,
                "xmp:CreateDate": capture_time,
                "xmpDM:shotDate": capture_time,
            }
        )
    make = selected.get("device-make")
    model = selected.get("device-model")
    if isinstance(make, str):
        attributes["tiff:Make"] = make
    if isinstance(model, str):
        attributes["tiff:Model"] = model
    latitude = _finite_float(selected.get("gps-latitude"))
    longitude = _finite_float(selected.get("gps-longitude"))
    if latitude is not None and longitude is not None:
        attributes.update(
            {
                "exif:GPSLatitude": _xmp_coordinate(latitude, latitude=True),
                "exif:GPSLongitude": _xmp_coordinate(longitude, latitude=False),
                "geo:lat": _decimal(latitude),
                "geo:long": _decimal(longitude),
            }
        )
    attribute_lines = "\n".join(
        f'   {name}="{escape(value, quote=True)}"' for name, value in sorted(attributes.items())
    )
    if attribute_lines:
        attribute_lines += "\n"
    blocks: list[str] = []
    creators = selected.get("creator")
    if isinstance(creators, list) and all(isinstance(value, str) for value in creators):
        blocks.append(_rdf_list("dc:creator", cast(list[str], creators), "Seq"))
    if tags:
        canonical_tags = tuple(sorted(set(tags)))
        blocks.append(_rdf_list("dc:subject", canonical_tags, "Bag"))
        blocks.append(_rdf_list("digiKam:TagsList", canonical_tags, "Seq"))
        blocks.append(
            _rdf_list(
                "lr:hierarchicalSubject",
                tuple(tag.replace("/", "|") for tag in canonical_tags),
                "Bag",
            )
        )
    if item.assertions:
        blocks.append(
            _rdf_list(
                "rh:assertions",
                tuple(
                    canonical_json_bytes(assertion.model_dump(mode="json")).decode("utf-8")
                    for assertion in item.assertions
                ),
                "Seq",
            )
        )
    body = "\n".join(blocks)
    if body:
        body = f"\n{body}"
    document = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '   xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '   xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"\n'
        '   xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
        '   xmlns:tiff="http://ns.adobe.com/tiff/1.0/"\n'
        '   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
        '   xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '   xmlns:digiKam="http://www.digikam.org/ns/1.0/"\n'
        '   xmlns:lr="http://ns.adobe.com/lightroom/1.0/"\n'
        '   xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"\n'
        '   xmlns:rh="https://nashspence.github.io/riverhog/ns/stove0-media-evidence/v1/"\n'
        f"{attribute_lines}  >{body}\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    )
    return document.encode("utf-8")


def ffmpeg_container_metadata_args(item: MediaProjectionItem) -> list[str]:
    selected = {value.name: value.value for value in item.selected}
    metadata: list[tuple[str, str]] = []
    capture = selected.get("capture-time")
    if isinstance(capture, str):
        metadata.extend((key, capture) for key in ("DATE", "creation_time"))
    creators = selected.get("creator")
    if isinstance(creators, list) and all(isinstance(value, str) for value in creators):
        joined = "; ".join(cast(list[str], creators))
        metadata.extend((key, joined) for key in ("ARTIST", "CREATOR"))
    scalar_fields: tuple[tuple[MediaFactName, str], ...] = (
        ("device-make", "MAKE"),
        ("device-model", "MODEL"),
    )
    for fact_name, key in scalar_fields:
        value = selected.get(fact_name)
        if isinstance(value, str):
            metadata.append((key, value))
    latitude = _finite_float(selected.get("gps-latitude"))
    longitude = _finite_float(selected.get("gps-longitude"))
    if latitude is not None and longitude is not None:
        metadata.extend(
            (
                ("LOCATION", f"{latitude:+.8f}{longitude:+.8f}/"),
                ("GPSLatitude", _decimal(latitude)),
                ("GPSLongitude", _decimal(longitude)),
            )
        )
    return [part for key, value in metadata for part in ("-metadata", f"{key}={value}")]


def _select_values(
    assertions: Sequence[MediaMetadataFact],
    policy: MediaProjectionPolicy,
) -> tuple[MediaProjectedValue, ...]:
    by_name: dict[MediaFactName, list[MediaMetadataFact]] = {}
    for assertion in assertions:
        by_name.setdefault(assertion.name, []).append(assertion)
    preferences = {item.name: item.fields for item in policy.field_preferences}
    selected: list[MediaProjectedValue] = []

    configured: dict[MediaFactName, JsonValue] = {}
    if policy.device_make is not None:
        configured["device-make"] = policy.device_make
    if policy.device_model is not None:
        configured["device-model"] = policy.device_model
    if policy.gps is not None:
        configured["gps-latitude"] = policy.gps.latitude
        configured["gps-longitude"] = policy.gps.longitude
    if policy.creators:
        configured["creator"] = list(policy.creators)
    for name, configured_value in configured.items():
        selected.append(MediaProjectedValue(name=name, value=configured_value, source="recipe"))

    for name in cast(tuple[MediaFactName, ...], tuple(sorted(by_name))):
        if name in configured or name == "container-format":
            continue
        candidates = by_name[name]
        if name == "creator":
            creators = tuple(
                dict.fromkeys(
                    value for candidate in candidates for value in _creator_values(candidate.value)
                )
            )
            if creators:
                selected.append(
                    MediaProjectedValue(
                        name=name,
                        value=list(creators),
                        source="observation",
                        evidence=tuple(
                            sorted(
                                {candidate.evidence for candidate in candidates},
                                key=lambda item: (item.artifact_id, item.field),
                            )
                        ),
                    )
                )
            continue
        candidate = _unambiguous_candidate(candidates, preferences.get(name, ()))
        if candidate is None:
            continue
        projected_value: JsonValue = candidate.value
        if name == "capture-time":
            normalized = _normalize_capture_time(projected_value)
            if normalized is None:
                continue
            projected_value = normalized
        selected.append(
            MediaProjectedValue(
                name=name,
                value=projected_value,
                source="observation",
                evidence=(candidate.evidence,),
            )
        )
    return tuple(sorted(selected, key=lambda item: item.name))


def _unambiguous_candidate(
    candidates: Sequence[MediaMetadataFact],
    preferred_fields: Sequence[str],
) -> MediaMetadataFact | None:
    for field in preferred_fields:
        preferred = [item for item in candidates if item.evidence.field == field]
        if preferred and len({_comparable_value(item) for item in preferred}) == 1:
            return preferred[0]
    if len({_comparable_value(item) for item in candidates}) == 1:
        return candidates[0]
    return None


def _comparable_value(fact: MediaMetadataFact) -> bytes:
    if fact.name == "capture-time":
        normalized = _normalize_capture_time(fact.value)
        if normalized is not None:
            return canonical_json_bytes(normalized)
    return canonical_json_bytes(fact.value)


_CAPTURE_RE = re.compile(
    r"^(?P<date>\d{4})(?P<sep>[:-])(?P<month>\d{2})(?P=sep)(?P<day>\d{2})"
    r"[ T](?P<time>\d{2}:\d{2}:\d{2})(?P<fraction>\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def _normalize_capture_time(value: JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    match = _CAPTURE_RE.fullmatch(value.strip())
    if match is None:
        return None
    timezone = match.group("tz") or ""
    if timezone and timezone != "Z" and ":" not in timezone:
        timezone = f"{timezone[:3]}:{timezone[3:]}"
    return (
        f"{match.group('date')}-{match.group('month')}-{match.group('day')}"
        f"T{match.group('time')}{match.group('fraction') or ''}{timezone}"
    )


def _creator_values(value: JsonValue) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(text for raw in values if isinstance(raw, str) and (text := raw.strip()))


def _finite_float(value: JsonValue | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _fact_key(fact: MediaMetadataFact) -> tuple[str, str, str, bytes]:
    return (
        fact.name,
        fact.evidence.artifact_id,
        fact.evidence.field,
        canonical_json_bytes(fact.value),
    )


def _decimal(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _xmp_coordinate(value: float, *, latitude: bool) -> str:
    direction = ("N" if value >= 0 else "S") if latitude else ("E" if value >= 0 else "W")
    absolute = abs(value)
    degrees = int(absolute)
    minutes = (absolute - degrees) * 60
    return f"{degrees},{minutes:.6f}{direction}"


def _rdf_list(name: str, values: Sequence[str], container: Literal["Bag", "Seq"]) -> str:
    lines = "\n".join(f"     <rdf:li>{escape(value)}</rdf:li>" for value in values)
    return f"   <{name}>\n    <rdf:{container}>\n{lines}\n    </rdf:{container}>\n   </{name}>"


__all__ = [
    "MEDIA_PROJECTION_FORMAT",
    "MediaArchiveProjection",
    "MediaArchiveProjectionPayload",
    "MediaFieldPreference",
    "MediaGps",
    "MediaProjectedValue",
    "MediaProjectionItem",
    "MediaProjectionPolicy",
    "RetainedXmpSidecar",
    "ffmpeg_container_metadata_args",
    "render_projection_xmp",
    "resolve_media_archive_projection",
]
