"""Content-opaque stove0 recipe policy and deterministic planning."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    ObservationEvidence,
    ObservationRequest,
    ObservationRequestPayload,
    OperationRef,
    RecipeRef,
    WorkflowPlan,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
)
from stove0_target_support import (
    InputArtifact,
    OperationContract,
    TargetPreflightRequest,
)

from stove0_core.coordinator import ObserverPort, TargetPort
from stove0_core.work_state import WorkInapplicable

OperationPlanCompiler = Callable[
    [WorkIdentity],
    tuple[Mapping[str, JsonValue], Mapping[str, JsonValue]],
]


class RecipeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRule(RecipeModel):
    glob: str = "*"
    role: str = "stove0.source/v1"
    media_type: str | None = None


class ObserverUse(RecipeModel):
    registration_id: str
    contract_id: str
    artifact_rules: tuple[ArtifactRule, ...] = (ArtifactRule(),)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    maximum_result_bytes: int = Field(default=1024 * 1024, ge=1, le=64 * 1024 * 1024)
    retrieval_policy: Literal["available-only", "allow"] = "available-only"


class FactPredicate(RecipeModel):
    observation_contract_id: str
    pointer: str = Field(pattern=r"^(?:|/(?:[^~/]|~[01])*)$")
    operator: Literal["equals", "not-equals", "contains", "exists"] = "equals"
    value: JsonValue = None


class RecipeRoute(RecipeModel):
    id: str
    when: tuple[FactPredicate, ...] = ()
    operation_id: str
    target_registration_id: str
    artifact_rules: tuple[ArtifactRule, ...] = (ArtifactRule(),)
    intent: dict[str, JsonValue] = Field(default_factory=dict)
    target_options: dict[str, JsonValue] = Field(default_factory=dict)
    input_retrieval_policy: Literal["available-only", "allow"] = "available-only"
    output_tags: tuple[str, ...] = Field(min_length=1)
    retirement_policy: Literal["retain", "retire-after-verified-output"] = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)


class RecipeDefinition(RecipeModel):
    id: str
    revision: int = Field(ge=1)
    input_tags: tuple[str, ...] = Field(min_length=1)
    event_input_closure: Literal["single-finalized-collection"] = "single-finalized-collection"
    observers: tuple[ObserverUse, ...] = ()
    routes: tuple[RecipeRoute, ...] = Field(min_length=1)
    allow_derived_inputs: bool = False

    @model_validator(mode="after")
    def canonical_members(self) -> Self:
        if self.input_tags != tuple(sorted(set(self.input_tags))):
            raise ValueError("recipe input tags must be unique and canonical")
        route_ids = [route.id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("recipe route IDs must be unique")
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json", by_alias=True, exclude_none=True))

    @property
    def ref(self) -> RecipeRef:
        return RecipeRef(id=self.id, revision=self.revision, sha256=self.sha256)


class RecipeCatalog(RecipeModel):
    format: Literal["stove0-recipes/v1"] = "stove0-recipes/v1"
    operations: tuple[OperationContract, ...]
    recipes: tuple[RecipeDefinition, ...]

    @model_validator(mode="after")
    def unique_recipes(self) -> Self:
        operation_ids = [operation.id for operation in self.operations]
        if operation_ids != sorted(operation_ids) or len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation contracts must be unique and ordered by ID")
        identities = [(recipe.id, recipe.revision) for recipe in self.recipes]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("recipes must be unique and ordered by ID and revision")
        return self

    def operation(self, operation_id: str) -> OperationContract:
        for operation in self.operations:
            if operation.id == operation_id:
                return operation
        raise KeyError(operation_id)

    @classmethod
    def load(cls, path: Path) -> RecipeCatalog:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(value)

    def recipe(self, recipe_id: str, revision: int | None = None) -> RecipeDefinition:
        matches = [
            recipe
            for recipe in self.recipes
            if recipe.id == recipe_id and (revision is None or recipe.revision == revision)
        ]
        if not matches:
            raise KeyError(recipe_id)
        return max(matches, key=lambda recipe: recipe.revision)

    def matching(self, tags: Sequence[str]) -> tuple[RecipeDefinition, ...]:
        available = set(tags)
        return tuple(
            recipe for recipe in self.recipes if set(recipe.input_tags).issubset(available)
        )


class RecipePlanner:
    """Production planning authority over metadata and bounded observation facts."""

    def __init__(
        self,
        *,
        catalog: RecipeCatalog,
        riverhog: ApiClient,
        observers: ObserverPort,
        targets: TargetPort,
        operation_compilers: Mapping[str, OperationPlanCompiler] | None = None,
    ) -> None:
        self.catalog = catalog
        self.riverhog = riverhog
        self.observers = observers
        self.targets = targets
        self.operation_compilers = dict(operation_compilers or {})

    def create_work(
        self,
        recipe_id: str,
        roots: Sequence[CollectionRootRef],
        *,
        revision: int | None = None,
        effective_intent: Mapping[str, JsonValue] | None = None,
    ) -> WorkIdentity:
        recipe = self.catalog.recipe(recipe_id, revision)
        return WorkIdentity.seal(
            WorkPayload(
                recipe=recipe.ref,
                inputs=tuple(sorted(roots, key=lambda root: root.collection_id)),
                effective_intent=dict(effective_intent or {}),
            )
        )

    def observation_requests(self, work: WorkIdentity) -> tuple[ObservationRequest, ...]:
        recipe = self._recipe(work)
        requests: list[ObservationRequest] = []
        inventory = self._inventory(work)
        for use in recipe.observers:
            descriptor = self.observers.descriptor(use.registration_id)
            support = descriptor.support_for(use.contract_id)
            subjects = _subjects(inventory, use.artifact_rules)
            if not subjects:
                continue
            requests.append(
                ObservationRequest.seal(
                    ObservationRequestPayload(
                        work_id=work.work_id,
                        observer_registration_id=use.registration_id,
                        observer_descriptor_sha256=descriptor.descriptor_sha256,
                        observer_contract_id=support.contract_id,
                        observer_contract_sha256=support.contract_sha256,
                        subjects=subjects,
                        options=use.options,
                        timeout_seconds=use.timeout_seconds,
                        maximum_result_bytes=use.maximum_result_bytes,
                        retrieval_policy=use.retrieval_policy,
                    )
                )
            )
        return tuple(sorted(requests, key=lambda request: request.request_id))

    def workflow_plan(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
    ) -> WorkflowPlan | WorkInapplicable:
        recipe = self._recipe(work)
        route = next(
            (
                candidate
                for candidate in recipe.routes
                if all(_predicate_matches(predicate, observations) for predicate in candidate.when)
            ),
            None,
        )
        if route is None:
            return WorkInapplicable(
                code="no-matching-route",
                message="No configured recipe route accepted the immutable inputs.",
            )
        target = self.targets.contract(route.target_registration_id)
        operation = self.catalog.operation(route.operation_id)
        support = target.support_for(operation.id)
        if support.operation_contract_sha256 != operation.contract_sha256:
            raise RuntimeError("target supports another revision of the recipe operation")
        _intent, compiled_options = self._compile_operation(work, operation.id)
        requested_options = {**route.target_options, **compiled_options}
        return WorkflowPlan.seal(
            WorkflowPlanPayload(
                work=work,
                observations=observations,
                operation=OperationRef(
                    id=operation.id,
                    sha256=operation.contract_sha256,
                ),
                target_registration_id=route.target_registration_id,
                target_contract_sha256=target.contract_sha256,
                requested_target_options=requested_options,
                input_retrieval_policy=route.input_retrieval_policy,
                output_tags=tuple(sorted(route.output_tags)),
                retirement_policy=route.retirement_policy,
                retirement_grace_seconds=route.retirement_grace_seconds,
                output_policy={"route_id": route.id},
            )
        )

    def target_preflight_request(self, plan: WorkflowPlan) -> TargetPreflightRequest:
        recipe = self._recipe(plan.work)
        route_id = str(plan.output_policy.get("route_id") or "")
        route = next((candidate for candidate in recipe.routes if candidate.id == route_id), None)
        if route is None:
            raise RuntimeError("sealed workflow plan refers to an unknown recipe route")
        inventory = self._inventory(plan.work)
        artifacts = _target_inputs(inventory, route.artifact_rules)
        compiled_intent, compiled_options = self._compile_operation(
            plan.work,
            plan.operation.id,
        )
        intent = {**route.intent, **compiled_intent}
        expected_options = {**route.target_options, **compiled_options}
        if expected_options != plan.requested_target_options:
            raise RuntimeError("compiled target options differ from the sealed workflow plan")
        return TargetPreflightRequest(
            operation_id=plan.operation.id,
            operation_contract_sha256=plan.operation.sha256,
            inputs=artifacts,
            intent=intent,
            target_options=plan.requested_target_options,
        )

    def _compile_operation(
        self,
        work: WorkIdentity,
        operation_id: str,
    ) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        compiler = self.operation_compilers.get(operation_id)
        if compiler is not None:
            intent, options = compiler(work)
            return dict(intent), dict(options)
        intent = dict(work.effective_intent)
        raw_options = intent.pop("target_options", {})
        if not isinstance(raw_options, Mapping):
            raise ValueError("effective target_options must be a JSON object")
        return intent, cast(dict[str, JsonValue], dict(raw_options))

    def operation_contract(self, operation: OperationRef) -> OperationContract:
        contract = self.catalog.operation(operation.id)
        if contract.contract_sha256 != operation.sha256:
            raise RuntimeError("operation contract differs from the sealed identity")
        return contract

    def _recipe(self, work: WorkIdentity) -> RecipeDefinition:
        recipe = self.catalog.recipe(work.recipe.id, work.recipe.revision)
        if recipe.sha256 != work.recipe.sha256:
            raise RuntimeError("configured recipe differs from the immutable work identity")
        return recipe

    def _inventory(self, work: WorkIdentity) -> tuple[dict[str, object], ...]:
        rows: list[dict[str, object]] = []
        for root in work.inputs:
            current = self.riverhog.get_collection(root.collection_id)
            if (
                str(current.get("manifest_sha256") or "") != root.manifest_sha256
                or str(current.get("content_etag") or "") != root.content_etag
            ):
                raise RuntimeError(f"collection root changed: {root.collection_id}")
            page = self.riverhog.search(
                collection=root.collection_id,
                all_items=True,
                sort="file_ref",
                order="asc",
            )
            for raw in page.get("files", []):
                if not isinstance(raw, Mapping):
                    raise RuntimeError("Riverhog returned an invalid artifact inventory")
                path = str(raw.get("path") or "")
                if path.startswith("riverhog/"):
                    continue
                rows.append(
                    {
                        "collection": root,
                        "path": path,
                        "bytes": int(raw.get("bytes") or 0),
                        "sha256": str(raw.get("sha256") or ""),
                    }
                )
        return tuple(rows)


def _subjects(
    inventory: Sequence[Mapping[str, object]],
    rules: Sequence[ArtifactRule],
) -> tuple[ArtifactSubject, ...]:
    subjects: list[ArtifactSubject] = []
    for raw in inventory:
        rule = _artifact_rule(str(raw["path"]), rules)
        if rule is None:
            continue
        root = cast(CollectionRootRef, raw["collection"])
        byte_count = raw["bytes"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise RuntimeError("Riverhog returned an invalid artifact byte count")
        artifact_id = (
            "a-"
            + canonical_json_sha256({"collection_id": root.collection_id, "path": raw["path"]})[:32]
        )
        subjects.append(
            ArtifactSubject(
                id=artifact_id,
                role=rule.role,
                collection=root,
                path=str(raw["path"]),
                bytes=byte_count,
                sha256=str(raw["sha256"]),
                media_type=rule.media_type,
            )
        )
    return tuple(sorted(subjects, key=lambda subject: subject.id))


def _target_inputs(
    inventory: Sequence[Mapping[str, object]],
    rules: Sequence[ArtifactRule],
) -> tuple[InputArtifact, ...]:
    return tuple(
        InputArtifact(
            id=subject.id,
            role=subject.role,
            collection=subject.collection,
            path=subject.path,
            bytes=subject.bytes,
            sha256=subject.sha256,
            media_type=subject.media_type,
        )
        for subject in _subjects(inventory, rules)
    )


def _artifact_rule(path: str, rules: Sequence[ArtifactRule]) -> ArtifactRule | None:
    return next((rule for rule in rules if fnmatch.fnmatchcase(path, rule.glob)), None)


def _predicate_matches(
    predicate: FactPredicate,
    observations: Sequence[ObservationEvidence],
) -> bool:
    matches = [
        evidence.result
        for evidence in observations
        if evidence.request.observer_contract_id == predicate.observation_contract_id
    ]
    if len(matches) != 1 or matches[0].state != "observed" or matches[0].facts is None:
        return False
    present, value = _json_pointer(matches[0].facts, predicate.pointer)
    if predicate.operator == "exists":
        return present is bool(predicate.value)
    if not present:
        return False
    if predicate.operator == "equals":
        return value == predicate.value
    if predicate.operator == "not-equals":
        return value != predicate.value
    if predicate.operator == "contains":
        return isinstance(value, list) and predicate.value in value
    raise AssertionError(predicate.operator)


def _json_pointer(document: JsonValue, pointer: str) -> tuple[bool, JsonValue]:
    current = document
    if pointer == "":
        return True, current
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


__all__ = [
    "ArtifactRule",
    "FactPredicate",
    "ObserverUse",
    "OperationPlanCompiler",
    "RecipeCatalog",
    "RecipeDefinition",
    "RecipePlanner",
    "RecipeRoute",
]
