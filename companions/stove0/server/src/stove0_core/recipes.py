"""Content-opaque stove0 recipe policy and deterministic planning."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Literal, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchPlan,
    BranchSetDecision,
    BranchSetPlan,
    BranchWorkBinding,
    CollectionRootRef,
    JoinDeclaration,
    JoinMemberDeclaration,
    JoinWorkBinding,
    ObservationEvidence,
    ObservationRequest,
    ObservationRequestPayload,
    OperationRef,
    RecipeRef,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
)
from stove0_target_protocol import (
    InputArtifact,
    OperationContract,
    TargetPreflightRequest,
)

from stove0_core.coordinator import ObserverPort, TargetPort
from stove0_core.work_state import WorkInapplicable


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


class OperationProjection(RecipeModel):
    """One bounded JSON-pointer copy into an operation request."""

    source: Literal["work-effective-intent", "work-evaluation"]
    source_pointer: str = Field(pattern=r"^(?:|/(?:[^~/]|~[01])*(?:/(?:[^~/]|~[01])*)*)$")
    destination: Literal["intent", "target-options"]
    destination_pointer: str = Field(pattern=r"^(?:|/(?:[^~/]|~[01])*(?:/(?:[^~/]|~[01])*)*)$")


class RecipeRoute(RecipeModel):
    id: str
    when: tuple[FactPredicate, ...] = ()
    operation_id: str
    target_registration_id: str
    artifact_rules: tuple[ArtifactRule, ...] = (ArtifactRule(),)
    intent: dict[str, JsonValue] = Field(default_factory=dict)
    target_options: dict[str, JsonValue] = Field(default_factory=dict)
    projections: tuple[OperationProjection, ...] = ()
    input_retrieval_policy: Literal["available-only", "allow"] = "available-only"
    output_tags: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_projections(self) -> Self:
        _validate_projections(self.projections)
        return self


class RecipeJoinMember(RecipeModel):
    branch_id: str
    output_roles: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_roles(self) -> Self:
        if self.output_roles != tuple(sorted(set(self.output_roles))):
            raise ValueError("join output roles must be unique and canonical")
        return self


class RecipeJoin(RecipeModel):
    id: str
    members: tuple[RecipeJoinMember, ...] = Field(min_length=2)
    operation_id: str
    target_registration_id: str
    intent: dict[str, JsonValue] = Field(default_factory=dict)
    target_options: dict[str, JsonValue] = Field(default_factory=dict)
    projections: tuple[OperationProjection, ...] = ()
    input_retrieval_policy: Literal["available-only", "allow"] = "available-only"
    output_tags: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_projections(self) -> Self:
        _validate_projections(self.projections)
        return self


class RecipeDefinition(RecipeModel):
    id: str
    revision: int = Field(ge=1)
    input_tags: tuple[str, ...] = Field(min_length=1)
    event_input_closure: Literal["single-finalized-collection"] = "single-finalized-collection"
    observers: tuple[ObserverUse, ...] = ()
    routes: tuple[RecipeRoute, ...] = Field(min_length=1)
    allow_derived_inputs: bool = False
    source_retirement_policy: Literal["retain", "retire-after-verified-output"] = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    join: RecipeJoin | None = None

    @model_validator(mode="after")
    def canonical_members(self) -> Self:
        if self.input_tags != tuple(sorted(set(self.input_tags))):
            raise ValueError("recipe input tags must be unique and canonical")
        route_ids = [route.id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("recipe route IDs must be unique")
        if self.join is not None:
            member_ids = [member.branch_id for member in self.join.members]
            if member_ids != sorted(member_ids) or len(member_ids) != len(set(member_ids)):
                raise ValueError("join members must be unique and ordered by branch ID")
            unknown = sorted(set(member_ids) - set(route_ids))
            if unknown:
                raise ValueError("join members reference unknown route IDs: " + ", ".join(unknown))
        if self.source_retirement_policy == "retain" and self.retirement_grace_seconds:
            raise ValueError("retain recipes cannot declare a retirement grace period")
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
    def valid_catalog(self) -> Self:
        operation_ids = [operation.id for operation in self.operations]
        if operation_ids != sorted(operation_ids) or len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation contracts must be unique and ordered by ID")
        identities = [(recipe.id, recipe.revision) for recipe in self.recipes]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("recipes must be unique and ordered by ID and revision")
        operations = {operation.id: operation for operation in self.operations}
        for recipe in self.recipes:
            referenced = [route.operation_id for route in recipe.routes]
            if recipe.join is not None:
                referenced.append(recipe.join.operation_id)
            unknown = sorted(set(referenced) - set(operations))
            if unknown:
                raise ValueError(
                    f"recipe {recipe.id} references unknown operation(s): " + ", ".join(unknown)
                )
            if recipe.source_retirement_policy == "retire-after-verified-output":
                unsafe = sorted(
                    route.id
                    for route in recipe.routes
                    if not operations[route.operation_id].source_retirement_permitted
                )
                if unsafe:
                    raise ValueError(
                        f"recipe {recipe.id} retires its source but branch operation(s) do not "
                        "authorize retirement: " + ", ".join(unsafe)
                    )
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
    ) -> None:
        self.catalog = catalog
        self.riverhog = riverhog
        self.observers = observers
        self.targets = targets

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
        if work.fork_join is not None:
            return ()
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
    ) -> BranchSetDecision | WorkInapplicable:
        if work.fork_join is not None:
            raise RuntimeError("branch and join workflow plans are sealed by their parent plan")
        recipe = self._recipe(work)
        inventory = self._inventory(work)
        selected: list[tuple[RecipeRoute, ArtifactSelection]] = []
        for route in recipe.routes:
            if not all(_predicate_matches(predicate, observations) for predicate in route.when):
                continue
            artifacts = _subjects(inventory, route.artifact_rules)
            if not artifacts:
                continue
            selected.append((route, ArtifactSelection.seal(artifacts)))
        selected.sort(key=lambda item: item[0].id)
        if not selected:
            return WorkInapplicable(
                code="no-matching-route",
                message="No configured recipe branch accepted the immutable inputs.",
            )

        selected_ids = {route.id for route, _selection in selected}
        if recipe.join is not None:
            missing = [
                member.branch_id
                for member in recipe.join.members
                if member.branch_id not in selected_ids
            ]
            if missing:
                return WorkInapplicable(
                    code="join-members-inapplicable",
                    message=(
                        "The configured exact join requires branches not selected by the "
                        "immutable observations: " + ", ".join(missing)
                    ),
                )

        if recipe.source_retirement_policy == "retire-after-verified-output":
            uncovered = _uncovered_inventory(inventory, selected)
            if uncovered:
                return WorkInapplicable(
                    code="unsafe-retirement-coverage",
                    message=(
                        "Source retirement requires the selected branch artifacts to cover "
                        "the complete immutable input inventory: " + ", ".join(uncovered[:10])
                    ),
                )
            for route, selection in selected:
                problem = _operation_input_problem(
                    self.catalog.operation(route.operation_id),
                    selection,
                )
                if problem is not None:
                    return WorkInapplicable(
                        code="unsafe-retirement-operation-inputs",
                        message=f"Unsafe retirement inputs for branch {route.id}: {problem}",
                    )

        decision_sha256 = canonical_json_sha256(
            {
                "format": "stove0-routing-decision/v1",
                "parent_work_id": work.work_id,
                "recipe": recipe.ref.model_dump(mode="json"),
                "evidence_sha256s": sorted(
                    evidence.result.result_sha256 for evidence in observations
                ),
                "branches": [
                    {
                        "branch_id": route.id,
                        "artifact_selection_sha256": selection.selection_sha256,
                    }
                    for route, selection in selected
                ],
                "join_members": (
                    [member.model_dump(mode="json") for member in recipe.join.members]
                    if recipe.join is not None
                    else []
                ),
            }
        )
        branches = tuple(
            self._branch_plan(
                parent=work,
                observations=observations,
                route=route,
                selection=selection,
                decision_sha256=decision_sha256,
                recipe=recipe,
            )
            for route, selection in selected
        )
        join = self._join_declaration(work, recipe.join) if recipe.join is not None else None
        documents = {selection.selection_sha256: selection for _route, selection in selected}
        selections = tuple(documents[digest] for digest in sorted(documents))
        return BranchSetDecision(
            plan=BranchSetPlan.seal(
                parent_work=work,
                decision_sha256=decision_sha256,
                evidence_sha256s=tuple(
                    sorted(evidence.result.result_sha256 for evidence in observations)
                ),
                branches=branches,
                join=join,
                retirement_policy=recipe.source_retirement_policy,
                retirement_grace_seconds=recipe.retirement_grace_seconds,
                selections=documents,
            ),
            selections=selections,
        )

    def _branch_plan(
        self,
        *,
        parent: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        route: RecipeRoute,
        selection: ArtifactSelection,
        decision_sha256: str,
        recipe: RecipeDefinition,
    ) -> BranchPlan:
        target = self.targets.contract(route.target_registration_id)
        operation = self.catalog.operation(route.operation_id)
        support = target.support_for(operation.id)
        if support.operation_contract_sha256 != operation.contract_sha256:
            raise RuntimeError("target supports another revision of the recipe operation")
        compiled_intent, compiled_options = self._project_operation(parent, route.projections)
        effective_intent = {**route.intent, **compiled_intent}
        return BranchPlan.build(
            parent_work=parent,
            branch_id=route.id,
            decision_sha256=decision_sha256,
            selection=selection,
            recipe=recipe.ref,
            effective_intent=effective_intent,
            workflow_intent=WorkflowPlanIntent(
                operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
                target_registration_id=route.target_registration_id,
                target_contract_sha256=target.contract_sha256,
                requested_target_options={**route.target_options, **compiled_options},
                input_retrieval_policy=route.input_retrieval_policy,
                output_tags=tuple(sorted(route.output_tags)),
                retirement_policy="retain",
                output_policy={
                    "route_id": route.id,
                    "branch_id": route.id,
                    "artifact_selection_sha256": selection.selection_sha256,
                },
            ),
            observations=observations,
        )

    def _join_declaration(
        self,
        parent: WorkIdentity,
        join: RecipeJoin,
    ) -> JoinDeclaration:
        target = self.targets.contract(join.target_registration_id)
        operation = self.catalog.operation(join.operation_id)
        support = target.support_for(operation.id)
        if support.operation_contract_sha256 != operation.contract_sha256:
            raise RuntimeError("join target supports another revision of the recipe operation")
        compiled_intent, compiled_options = self._project_operation(parent, join.projections)
        return JoinDeclaration.seal(
            members=tuple(
                JoinMemberDeclaration(
                    branch_id=member.branch_id,
                    output_roles=member.output_roles,
                )
                for member in join.members
            ),
            recipe=parent.recipe,
            effective_intent={**join.intent, **compiled_intent},
            workflow_intent=WorkflowPlanIntent(
                operation=OperationRef(id=operation.id, sha256=operation.contract_sha256),
                target_registration_id=join.target_registration_id,
                target_contract_sha256=target.contract_sha256,
                requested_target_options={**join.target_options, **compiled_options},
                input_retrieval_policy=join.input_retrieval_policy,
                output_tags=tuple(sorted(join.output_tags)),
                retirement_policy="retain",
                output_policy={"route_id": join.id, "join_id": join.id},
            ),
        )

    def target_preflight_request(
        self,
        plan: WorkflowPlan,
        selections: Mapping[str, ArtifactSelection],
    ) -> TargetPreflightRequest:
        self._recipe(plan.work)
        binding = plan.work.fork_join
        if isinstance(binding, BranchWorkBinding):
            selection = selections.get(binding.artifact_selection_sha256)
            if selection is None:
                raise RuntimeError("branch preflight is missing its exact artifact selection")
            artifacts = _target_inputs_from_selection(selection)
        elif isinstance(binding, JoinWorkBinding):
            artifacts = _join_target_inputs(binding, selections)
        else:
            raise RuntimeError("target execution requires explicit branch or join work")
        return TargetPreflightRequest(
            operation_id=plan.operation.id,
            operation_contract_sha256=plan.operation.sha256,
            inputs=artifacts,
            intent=plan.work.effective_intent,
            target_options=plan.requested_target_options,
        )

    def _project_operation(
        self,
        work: WorkIdentity,
        projections: tuple[OperationProjection, ...],
    ) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        if projections:
            intent: dict[str, JsonValue] = {}
            options: dict[str, JsonValue] = {}
            sources: dict[str, JsonValue] = {
                "work-effective-intent": work.effective_intent,
                "work-evaluation": (
                    work.evaluation.model_dump(mode="json") if work.evaluation is not None else None
                ),
            }
            for projection in projections:
                value = _projection_value(sources[projection.source], projection.source_pointer)
                destination = intent if projection.destination == "intent" else options
                _json_pointer_set(destination, projection.destination_pointer, deepcopy(value))
            return intent, options
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


def _validate_projections(projections: tuple[OperationProjection, ...]) -> None:
    keys = [(item.destination, item.destination_pointer) for item in projections]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError("operation projections must be unique and canonically ordered")
    for index, (destination, pointer) in enumerate(keys):
        for other_destination, other_pointer in keys[index + 1 :]:
            if other_destination != destination:
                continue
            if other_pointer.startswith(f"{pointer}/"):
                raise ValueError("operation projection destinations must not overlap")


def _pointer_parts(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))


def _projection_value(document: JsonValue, pointer: str) -> JsonValue:
    current = document
    for part in _pointer_parts(pointer):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"operation projection source does not exist: {pointer}")
    return current


def _json_pointer_set(document: dict[str, JsonValue], pointer: str, value: JsonValue) -> None:
    parts = _pointer_parts(pointer)
    if not parts:
        if not isinstance(value, dict):
            raise ValueError("root operation projection requires a JSON object")
        document.update(value)
        return
    current = document
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"operation projection destination is not an object: {pointer}")
        current = child
    current[parts[-1]] = value


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


def _uncovered_inventory(
    inventory: Sequence[Mapping[str, object]],
    selected: Sequence[tuple[RecipeRoute, ArtifactSelection]],
) -> list[str]:
    covered = {
        (
            artifact.collection.collection_id,
            artifact.collection.manifest_sha256,
            artifact.path,
            artifact.bytes,
            artifact.sha256,
        )
        for _route, selection in selected
        for artifact in selection.artifacts
    }
    return sorted(
        str(raw["path"])
        for raw in inventory
        if (
            cast(CollectionRootRef, raw["collection"]).collection_id,
            cast(CollectionRootRef, raw["collection"]).manifest_sha256,
            str(raw["path"]),
            raw["bytes"],
            str(raw["sha256"]),
        )
        not in covered
    )


def _operation_input_problem(
    operation: OperationContract,
    selection: ArtifactSelection,
) -> str | None:
    counts: dict[str, int] = {}
    for artifact in selection.artifacts:
        counts[artifact.role] = counts.get(artifact.role, 0) + 1
    contracts = {item.role: item for item in operation.inputs}
    unsupported = sorted(set(counts) - set(contracts))
    if unsupported:
        return "unsupported role(s): " + ", ".join(unsupported)
    for role, contract in contracts.items():
        count = counts.get(role, 0)
        if count < contract.minimum or (contract.maximum is not None and count > contract.maximum):
            return f"input role cardinality is invalid: {role}"
    return None


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


def _target_inputs_from_selection(
    selection: ArtifactSelection,
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
        for subject in selection.artifacts
    )


def _join_target_inputs(
    binding: JoinWorkBinding,
    selections: Mapping[str, ArtifactSelection],
) -> tuple[InputArtifact, ...]:
    artifacts: list[InputArtifact] = []
    for member in binding.members:
        selection = selections.get(member.artifact_selection_sha256)
        if selection is None:
            raise RuntimeError(
                f"join preflight is missing the exact {member.branch_id} artifact selection"
            )
        for subject in selection.artifacts:
            artifact_id = (
                "j-"
                + canonical_json_sha256(
                    {
                        "branch_id": member.branch_id,
                        "selection_sha256": member.artifact_selection_sha256,
                        "artifact_id": subject.id,
                    }
                )[:32]
            )
            artifacts.append(
                InputArtifact(
                    id=artifact_id,
                    role=subject.role,
                    collection=subject.collection,
                    path=subject.path,
                    bytes=subject.bytes,
                    sha256=subject.sha256,
                    media_type=subject.media_type,
                )
            )
    return tuple(sorted(artifacts, key=lambda item: item.id))


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
    "BranchSetDecision",
    "FactPredicate",
    "ObserverUse",
    "OperationProjection",
    "RecipeCatalog",
    "RecipeDefinition",
    "RecipeJoin",
    "RecipeJoinMember",
    "RecipePlanner",
    "RecipeRoute",
]
