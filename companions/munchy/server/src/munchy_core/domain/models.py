from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from munchy_api_client.routing import (
    PATH_PREDICATE_KEYS,
    PREDICATE_KEYS,
    normalize_exiftool_tag,
    sidecar_rule_fact_extractors,
)
from munchy_workflows.profiles import (
    EncodeProfile,
)
from munchy_workflows.review_sweep import (
    ensure_review_sweep_has_variants,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UploadState = Literal["pending", "partial", "uploaded", "consumed"]


OutputMode = Literal["video", "audio", "preserve"]


WorkflowMode = Literal["collection_archive", "review"]


HandoffDestination = str


TaskName = Literal["archive_video", "archive_audio", "qcut_video", "audio_review"]


HandoffFailureAction = Literal["preserve_for_resume", "cancel"]


DEFAULT_TASKS: tuple[TaskName, ...] = ("archive_video", "qcut_video", "audio_review")


DEFAULT_AUDIO_TASKS: tuple[TaskName, ...] = ("archive_audio",)


DEFAULT_REVIEW_CLIP_TARGET_SECONDS = 180


DEFAULT_REVIEW_CLIP_MIN_SECONDS = 6


DEFAULT_REVIEW_CLIP_MAX_SECONDS = 9


GPU_TARGET_TASKS = frozenset({"archive_video", "qcut_video", "audio_review"})


LifecycleEventType = Literal[
    "job.received",
    "review.handoff",
    "collection_archive.handoff",
    "archive.handoff",
    "job.issue",
    "job.upload_stalled",
    "job.succeeded",
]


def default_tasks() -> list[TaskName]:
    return list(DEFAULT_TASKS)


def tasks_require_gpu(tasks: Sequence[Any]) -> bool:
    return any(str(task) in GPU_TARGET_TASKS for task in tasks)


SAFE_GROUP_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


TERMINAL_JOB_STATES = {"succeeded", "failed", "canceled"}


JOB_LIST_SORT_COLUMNS = {
    "job_id": "job_id",
    "created_at": "created_at",
    "finished_at": "finished_at",
    "input_upload_id": "input_upload_id",
    "phase": "phase",
    "run_id": "run_id",
    "state": "state",
    "updated_at": "updated_at",
    "workflow_mode": "workflow_mode",
}


JOB_LIST_TERMINAL_FILTERS = {"active", "all", "terminal"}


JOB_SEARCH_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")


JOB_TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


JOB_TEMPLATE_LIST_SORT_COLUMNS = {
    "created_at": "created_at",
    "template_id": "template_id",
    "revision": "revision",
    "updated_at": "updated_at",
}


def validate_group_name(value: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."}:
        raise ValueError("group name must not be blank, '.', or '..'")
    if "/" in name or "\\" in name:
        raise ValueError("group name must be a single path segment")
    if any(ch not in SAFE_GROUP_NAME_CHARS for ch in name):
        raise ValueError(
            "group name may contain only letters, digits, dots, underscores, and dashes"
        )
    return name


def input_path_group(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 2:
        raise ValueError("input file paths must be '<group>/<file>'")
    return validate_group_name(parts[0])


def normalize_posix(value: str) -> str:
    path = str(value).strip().lstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("path must be normalized and relative")
    return path


def path_under(root: Path, relative: str | Path, *, label: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        raise RuntimeError(f"{label} escaped its configured root")
    return candidate


def normalize_output_mode(value: str | None) -> str:
    return str(value or "video")


class InputFileProvenanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["captured", "omitted"]
    journal_id: str | None = None
    current_state_id: str | None = None
    omission_reason: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> InputFileProvenanceSpec:
        if self.status == "captured":
            if not self.journal_id or not self.current_state_id or self.omission_reason is not None:
                raise ValueError("captured provenance requires journal_id and current_state_id")
        elif (
            not self.omission_reason
            or self.omission_reason != self.omission_reason.strip()
            or self.journal_id is not None
            or self.current_state_id is not None
        ):
            raise ValueError("omitted provenance requires only a visible omission_reason")
        return self


class PreflightInputFileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip().lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("path must be relative and normalized")
        return normalized


class InputFileSpec(PreflightInputFileSpec):
    sha256: str = Field(min_length=64, max_length=64)
    provenance: InputFileProvenanceSpec

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        lowered = value.lower()
        if len(lowered) != 64 or any(ch not in "0123456789abcdef" for ch in lowered):
            raise ValueError("sha256 must be a 64-character hex digest")
        return lowered


HANDOFF_OPTION_MODELS: dict[str, type[BaseModel]] = {}


class HandoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1, max_length=160)
    options: dict[str, Any] = Field(default_factory=dict)
    on_failure: HandoffFailureAction = "preserve_for_resume"

    @field_validator("destination")
    @classmethod
    def normalize_destination(cls, value: str) -> str:
        destination = value.strip().casefold().replace("-", "_")
        if destination not in HANDOFF_OPTION_MODELS:
            raise ValueError(
                "handoff destination must be one of: " + ", ".join(sorted(HANDOFF_OPTION_MODELS))
            )
        return destination

    @model_validator(mode="after")
    def validate_options(self) -> HandoffConfig:
        option_model = HANDOFF_OPTION_MODELS[self.destination]
        self.options = option_model.model_validate(self.options).model_dump(exclude_none=True)
        return self


class ReviewClipPlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_TARGET_SECONDS, ge=1)
    min_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_MIN_SECONDS, ge=1)
    max_seconds: int = Field(default=DEFAULT_REVIEW_CLIP_MAX_SECONDS, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> ReviewClipPlanConfig:
        if self.min_seconds > self.max_seconds:
            raise ValueError("clip_plan.min_seconds must be <= max_seconds")
        return self


class ReviewSweepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality: Any = None
    max_height: Any = None
    audio_bitrate: Any = None
    axes: dict[str, Any] | list[dict[str, Any]] | None = None
    variants: list[dict[str, Any]] = Field(default_factory=list)
    profile_id_template: str | None = Field(default=None, min_length=1, max_length=180)
    route_ids: list[str] = Field(default_factory=list)

    @field_validator("profile_id_template")
    @classmethod
    def validate_profile_id_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("review.sweep.profile_id_template must not be blank")
        return text

    @field_validator("route_ids")
    @classmethod
    def normalize_route_ids(cls, value: list[str]) -> list[str]:
        route_ids: list[str] = []
        for item in value:
            route_id = str(item).strip()
            if not route_id:
                raise ValueError("review.sweep.route_ids must not contain blanks")
            if route_id not in route_ids:
                route_ids.append(route_id)
        return route_ids

    @model_validator(mode="after")
    def validate_sweep(self) -> ReviewSweepConfig:
        try:
            ensure_review_sweep_has_variants(self.model_dump(exclude_none=True))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str | None = Field(default=None, min_length=1, max_length=180)
    profile_id: str | None = Field(default=None, min_length=1, max_length=180)
    clip_plan: ReviewClipPlanConfig | None = None
    sweep: ReviewSweepConfig | None = None

    @model_validator(mode="after")
    def validate_review_shape(self) -> ReviewConfig:
        if self.sweep is None:
            if not self.route_id or not self.profile_id:
                raise ValueError("review jobs require route_id and profile_id unless sweep is set")
            return self
        if self.route_id or self.profile_id:
            raise ValueError("review sweep jobs must not set top-level route_id or profile_id")
        return self


class SubmissionPreflightIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)


class SubmissionPreflightFailedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    source: str = Field(min_length=1, max_length=4096)
    issues: list[SubmissionPreflightIssue] = Field(default_factory=list)


class SubmissionPreflightFailureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(min_length=1, max_length=180)
    message: str = Field(min_length=1, max_length=1000)
    template_id: str = Field(min_length=1, max_length=160)
    workflow_mode: WorkflowMode
    group: str = Field(min_length=1, max_length=180)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    route_id: str | None = Field(default=None, min_length=1, max_length=180)
    profile_id: str | None = Field(default=None, min_length=1, max_length=180)
    files_total: int = Field(ge=0)
    failed_files_total: int = Field(ge=1)
    failed_files: list[SubmissionPreflightFailedFile] = Field(default_factory=list)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    event_context: dict[str, Any] | None = None

    @field_validator("group")
    @classmethod
    def validate_group(cls, value: str) -> str:
        return validate_group_name(value)


class MetadataProjectionDeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    make: str | None = None
    model: str | None = None

    @field_validator("make", "model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class MetadataProjectionGpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float
    altitude: float | None = None

    @model_validator(mode="after")
    def validate_position(self) -> MetadataProjectionGpsConfig:
        if not (-90.0 <= self.latitude <= 90.0):
            raise ValueError("metadata_projection.gps.latitude must be between -90 and 90")
        if not (-180.0 <= self.longitude <= 180.0):
            raise ValueError("metadata_projection.gps.longitude must be between -180 and 180")
        return self


class MetadataProjectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target: Literal["immich_xmp"] = "immich_xmp"
    allow_missing_capture_date: bool = False
    allow_missing_gps: bool = False
    allow_missing_device_make: bool = False
    allow_missing_device_model: bool = False
    allow_missing_creators: bool = False
    capture_date_sources: list[dict[str, Any]] | None = None
    gps_sources: list[dict[str, Any]] | None = None
    device: MetadataProjectionDeviceConfig = Field(default_factory=MetadataProjectionDeviceConfig)
    gps: MetadataProjectionGpsConfig | None = None
    creators: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    include_context_tags: bool = True

    @field_validator("creators", "tags")
    @classmethod
    def normalize_text_list(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("capture_date_sources")
    @classmethod
    def validate_capture_date_sources(
        cls,
        value: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        return validate_metadata_sources(
            value,
            label="metadata_projection.capture_date_sources",
            allowed={"embedded", "path_regex", "provenance_timestamp", "sidecar"},
        )

    @field_validator("gps_sources")
    @classmethod
    def validate_gps_sources(
        cls,
        value: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        return validate_metadata_sources(
            value,
            label="metadata_projection.gps_sources",
            allowed={"embedded", "sidecar"},
        )


MetadataProjectionSetting = MetadataProjectionConfig | Literal[False]


def validate_metadata_sources(
    value: list[dict[str, Any]] | None,
    *,
    label: str,
    allowed: set[str],
) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{label} entries must be tables")
        source_type = str(item.get("type") or "embedded").strip()
        if source_type not in allowed:
            raise ValueError(f"{label} type must be one of: {', '.join(sorted(allowed))}")
        normalized.append(dict(item))
    return normalized


class GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=default_tasks)
    encode_profile: EncodeProfile | None = None
    max_parallel_encodes: int | None = Field(default=None, ge=1, le=64)
    eager_pipeline_batches: int | None = Field(default=None, ge=1, le=64)
    metadata_projection: MetadataProjectionSetting = Field(default_factory=MetadataProjectionConfig)

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def normalize_preserve(self) -> GroupConfig:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video groups cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio groups cannot run archive_video or qcut_video")
        return self


class StorageGroupHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=list)
    eager_pipeline_batches: int | None = Field(default=None, ge=1, le=64)

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def normalize_preserve(self) -> StorageGroupHint:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video storage groups cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio storage groups cannot run archive_video or qcut_video")
        return self


class InputUploadStorageHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_mode: WorkflowMode
    handoff_destination: HandoffDestination
    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=list)
    groups: dict[str, StorageGroupHint] = Field(default_factory=dict)
    structured_routing: bool = False

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, StorageGroupHint],
    ) -> dict[str, StorageGroupHint]:
        return {validate_group_name(name): group for name, group in value.items()}

    @model_validator(mode="after")
    def normalize_preserve(self) -> InputUploadStorageHint:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.output_mode == "video" and "archive_audio" in self.tasks:
            raise ValueError("video input upload hints cannot run archive_audio")
        if self.output_mode == "audio" and any(
            task in self.tasks for task in ("archive_video", "qcut_video")
        ):
            raise ValueError("audio input upload hints cannot run archive_video or qcut_video")
        return self


def validate_routing_predicate(value: Mapping[str, Any], *, label: str) -> None:
    unknown = sorted(set(value) - PREDICATE_KEYS)
    if unknown:
        raise ValueError(f"{label} has unknown key(s): {', '.join(unknown)}")
    path_predicate = value.get("path")
    if isinstance(path_predicate, Mapping):
        path_unknown = sorted(set(path_predicate) - PATH_PREDICATE_KEYS)
        if path_unknown:
            raise ValueError(f"{label}.path has unknown key(s): {', '.join(path_unknown)}")
    for key in ("all", "any"):
        items = value.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"{label}.{key} must be a list")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise ValueError(f"{label}.{key}[{index}] must be a predicate")
            validate_routing_predicate(item, label=f"{label}.{key}[{index}]")
    not_item = value.get("not")
    if not_item is not None:
        if not isinstance(not_item, Mapping):
            raise ValueError(f"{label}.not must be a predicate")
        validate_routing_predicate(not_item, label=f"{label}.not")


def routing_predicate_facts(value: object) -> set[str]:
    if isinstance(value, list):
        facts: set[str] = set()
        for item in value:
            facts.update(routing_predicate_facts(item))
        return facts
    if not isinstance(value, Mapping):
        return set()
    facts = {str(value["fact"])} if isinstance(value.get("fact"), str) else set()
    for nested in value.values():
        if isinstance(nested, (Mapping, list)):
            facts.update(routing_predicate_facts(nested))
    return facts


class RoutingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    action: Literal["upload", "leave"] = "upload"
    group: str | None = Field(default=None, min_length=1, max_length=120)
    into: str | None = Field(default=None, min_length=1, max_length=512)
    when: dict[str, Any] = Field(default_factory=dict)

    @field_validator("group")
    @classmethod
    def validate_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_group_name(value)

    @field_validator("into")
    @classmethod
    def normalize_output_dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = normalize_posix(value.strip().rstrip("/"))
        if not text or any(part in {"", ".", ".."} for part in text.split("/")):
            raise ValueError("output directory must be normalized and relative")
        return text

    @field_validator("when")
    @classmethod
    def validate_when(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_routing_predicate(value, label="route.when")
        return value

    @model_validator(mode="after")
    def validate_route_semantics(self) -> RoutingRule:
        if self.action == "upload" and not self.group:
            raise ValueError("upload routes require group")
        if self.action == "leave" and self.group:
            raise ValueError("leave routes must not set group")
        if self.action == "leave" and self.into:
            raise ValueError("leave routes must not set into")
        return self


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_exiftool_tags: list[str] | None = None
    gates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pairings: list[dict[str, Any]] = Field(default_factory=list)
    sidecars: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[RoutingRule] = Field(default_factory=list)

    @field_validator("extra_exiftool_tags")
    @classmethod
    def normalize_extra_exiftool_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = normalize_exiftool_tag(item)
            if tag is None:
                raise ValueError("routing.extra_exiftool_tags must be non-empty tags")
            if tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
        return normalized

    @field_validator("routes")
    @classmethod
    def require_routes(cls, value: list[RoutingRule]) -> list[RoutingRule]:
        if not value:
            raise ValueError("routing.routes must contain at least one route")
        ids = [route.id for route in value]
        if len(ids) != len(set(ids)):
            raise ValueError("routing route ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_predicates(self) -> RoutingConfig:
        for name, gate in self.gates.items():
            validate_routing_predicate(gate, label=f"routing.gates.{name}")
        allowed_pairing_keys = {"id", "key", "prefer_same_stem", "still", "movie"}
        for index, pairing in enumerate(self.pairings):
            unknown = sorted(set(pairing) - allowed_pairing_keys)
            if unknown:
                raise ValueError(
                    f"routing.pairings[{index}] has unknown key(s): " + ", ".join(unknown)
                )
            if not str(pairing.get("id") or "").strip():
                raise ValueError(f"routing.pairings[{index}].id is required")
            for key in ("still", "movie"):
                predicate = pairing.get(key)
                if not isinstance(predicate, Mapping):
                    raise ValueError(f"routing.pairings[{index}].{key} must be a predicate")
                validate_routing_predicate(
                    predicate,
                    label=f"routing.pairings[{index}].{key}",
                )
        allowed_sidecar_keys = {
            "id",
            "facts",
            "format",
            "path",
            "paths",
            "primary",
            "sidecar",
        }
        for index, sidecar in enumerate(self.sidecars):
            unknown = sorted(set(sidecar) - allowed_sidecar_keys)
            if unknown:
                raise ValueError(
                    f"routing.sidecars[{index}] has unknown key(s): " + ", ".join(unknown)
                )
            if not str(sidecar.get("id") or "").strip():
                raise ValueError(f"routing.sidecars[{index}].id is required")
            if "path" in sidecar and "paths" in sidecar:
                raise ValueError(f"routing.sidecars[{index}] must use path or paths, not both")
            paths = sidecar.get("paths")
            if paths is not None and (
                not isinstance(paths, list)
                or not all(isinstance(item, str) and item.strip() for item in paths)
            ):
                raise ValueError(f"routing.sidecars[{index}].paths must be strings")
            path = sidecar.get("path")
            if path is not None and not (isinstance(path, str) and path.strip()):
                raise ValueError(f"routing.sidecars[{index}].path must be a string")
            facts = sidecar.get("facts")
            if facts is not None:
                if not isinstance(facts, Mapping):
                    raise ValueError(f"routing.sidecars[{index}].facts must be a table")
                unknown_fact_keys = sorted(set(facts) - {"source", "tags", "extractors"})
                if unknown_fact_keys:
                    raise ValueError(
                        f"routing.sidecars[{index}].facts has unknown key(s): "
                        + ", ".join(unknown_fact_keys)
                    )
                source = str(facts.get("source") or "exiftool").strip().casefold()
                if source != "exiftool":
                    raise ValueError(f"routing.sidecars[{index}].facts.source must be exiftool")
                tags = facts.get("tags")
                if (
                    not isinstance(tags, list)
                    or not tags
                    or not all(isinstance(item, str) and item.strip() for item in tags)
                ):
                    raise ValueError(
                        f"routing.sidecars[{index}].facts.tags must be non-empty strings"
                    )
                for tag in tags:
                    normalize_exiftool_tag(tag)
                extractors = facts.get("extractors")
                if extractors is not None:
                    if not isinstance(extractors, list):
                        raise ValueError(
                            f"routing.sidecars[{index}].facts.extractors must be a list"
                        )
                    sidecar_rule_fact_extractors(sidecar)
            for key in ("primary", "sidecar"):
                predicate = sidecar.get(key)
                if predicate is None:
                    continue
                if not isinstance(predicate, Mapping):
                    raise ValueError(f"routing.sidecars[{index}].{key} must be a predicate")
                validate_routing_predicate(
                    predicate,
                    label=f"routing.sidecars[{index}].{key}",
                )
        return self


class PreflightInputUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: Sequence[PreflightInputFileSpec]
    storage_hint: InputUploadStorageHint

    @field_validator("files")
    @classmethod
    def require_files(
        cls, value: Sequence[PreflightInputFileSpec]
    ) -> Sequence[PreflightInputFileSpec]:
        if not value:
            raise ValueError("at least one file is required")
        paths = [item.path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return value

    @model_validator(mode="after")
    def validate_path_shape(self) -> PreflightInputUploadRequest:
        if self.storage_hint.structured_routing:
            return self
        for item in self.files:
            input_path_group(item.path)
        return self


class CreateInputUploadRequest(PreflightInputUploadRequest):
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=160)
    files: list[InputFileSpec]


class CreateJobRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=180)
    input_upload_id: str | None = Field(default=None, min_length=1, max_length=180)
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    workflow_mode: WorkflowMode = "collection_archive"
    output_mode: OutputMode = "video"
    tasks: list[TaskName] = Field(default_factory=default_tasks)
    encode_profile: EncodeProfile | None = None
    groups: dict[str, GroupConfig] = Field(default_factory=dict)
    routing: RoutingConfig | None = None
    handoff: HandoffConfig
    review: ReviewConfig | None = None
    event_context: dict[str, Any] | None = None

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, value: list[TaskName]) -> list[TaskName]:
        return list(dict.fromkeys(value))

    @field_validator("groups")
    @classmethod
    def normalize_groups(
        cls,
        value: dict[str, GroupConfig],
    ) -> dict[str, GroupConfig]:
        return {validate_group_name(name): group for name, group in value.items()}

    @model_validator(mode="after")
    def validate_workflow_mode(self) -> CreateJobRequest:
        if self.output_mode == "preserve":
            self.tasks = []
        elif self.output_mode == "audio" and "tasks" not in self.model_fields_set:
            self.tasks = list(DEFAULT_AUDIO_TASKS)
        if self.routing is not None:
            if not self.groups:
                raise ValueError("routing requires explicit groups")
            group_names = set(self.groups)
            route_groups = {
                route.group
                for route in self.routing.routes
                if route.action == "upload" and route.group
            }
            missing = sorted(route_groups - group_names)
            if missing:
                raise ValueError("routing references unknown group(s): " + ", ".join(missing))
        task_lists = (
            [(name, group.output_mode, group.tasks) for name, group in self.groups.items()]
            if self.groups
            else [("default", self.output_mode, self.tasks)]
        )
        for name, output_mode, tasks in task_lists:
            if output_mode == "video" and "archive_audio" in tasks:
                raise ValueError(f"video group {name!r} cannot run archive_audio")
            if output_mode == "audio" and any(
                task in tasks for task in ("archive_video", "qcut_video")
            ):
                raise ValueError(f"audio group {name!r} cannot run archive_video or qcut_video")
        if self.workflow_mode == "review":
            if self.review is None:
                raise ValueError("review jobs require review config")
            reviewable_group_found = False
            for name, output_mode, tasks in task_lists:
                if any(task in tasks for task in ("archive_video", "archive_audio")):
                    raise ValueError(
                        f"review group {name!r} cannot run archive_video or archive_audio"
                    )
                has_review_task = any(task in tasks for task in ("qcut_video", "audio_review"))
                if has_review_task:
                    reviewable_group_found = True
                if output_mode == "preserve":
                    continue
                if not has_review_task:
                    raise ValueError(f"review group {name!r} requires qcut_video or audio_review")
            if not reviewable_group_found:
                raise ValueError("review jobs require at least one reviewable group")
            return self

        for name, output_mode, tasks in task_lists:
            if output_mode in {"video", "audio"} and not any(
                task in tasks for task in ("archive_video", "archive_audio")
            ):
                raise ValueError(
                    f"collection_archive group {name!r} requires archive_video or archive_audio"
                )
        return self


class JobTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=160)
    definition: dict[str, Any]
    enabled: bool = True

    @field_validator("template_id")
    @classmethod
    def validate_name(cls, value: str) -> str:
        template_id = value.strip()
        if not JOB_TEMPLATE_ID_RE.fullmatch(template_id):
            raise ValueError(
                "template_id must start with an alphanumeric character and contain only "
                "letters, digits, dots, underscores, and dashes"
            )
        return template_id


class JobTemplateReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    enabled: bool = True
    expected_revision: int = Field(ge=1)


class JobTemplateEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class SubmissionPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=160)
    inputs: dict[str, str] = Field(default_factory=dict)
    files: Sequence[PreflightInputFileSpec]
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    handoff_on_failure: HandoffFailureAction = "preserve_for_resume"
    event_context: dict[str, Any] | None = None

    @field_validator("template_id")
    @classmethod
    def validate_template(cls, value: str) -> str:
        name = value.strip()
        if not JOB_TEMPLATE_ID_RE.fullmatch(name):
            raise ValueError("template_id is not a valid job-template ID")
        return name

    @field_validator("files")
    @classmethod
    def require_files(
        cls, value: Sequence[PreflightInputFileSpec]
    ) -> Sequence[PreflightInputFileSpec]:
        if not value:
            raise ValueError("at least one file is required")
        paths = [item.path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return value

    @field_validator("inputs")
    @classmethod
    def normalize_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            name = str(raw_name).strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
                raise ValueError(f"invalid submission input name: {raw_name}")
            normalized[name] = str(raw_value).strip()
        return normalized


class SubmissionSpec(SubmissionPreflightRequest):
    files: list[InputFileSpec]


class CreateSubmissionRequest(SubmissionSpec):
    submission_id: str | None = Field(default=None, min_length=1, max_length=160)


class CreateApplicationKeyRequest(BaseModel):
    permissions: list[str] = Field(min_length=1)
    expires_in_seconds: int | None = Field(default=None, ge=1)
