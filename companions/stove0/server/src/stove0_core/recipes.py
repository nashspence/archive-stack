"""Content-opaque stove0 recipe policy and deterministic planning."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import cast

from pydantic import JsonValue
from riverhog_api_client import ApiClient
from riverhog_protocol.collection_workflows import canonical_json_sha256
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSubject,
    BranchDeclaration,
    BranchPlan,
    BranchSetDecision,
    BranchSetPlan,
    BranchWorkBinding,
    CollectionRootRef,
    CoordinationBranchPlan,
    JoinDeclaration,
    JoinMemberDeclaration,
    JoinWorkBinding,
    ObservationEvidence,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    OperationRef,
    WorkflowPlan,
    WorkflowPlanIntent,
    WorkIdentity,
    WorkPayload,
)
from stove0_recipe_config import (
    ArtifactAssociation,
    ArtifactFactBinding,
    ArtifactRule,
    FactPredicate,
    ObserverUse,
    OperationProjection,
    RecipeBranch,
    RecipeCatalog,
    RecipeCoordinationRoute,
    RecipeDefinition,
    RecipeJoin,
    RecipeJoinMember,
    RecipeRoute,
)
from stove0_target_protocol import (
    InputArtifact,
    OperationContract,
    TargetPreflightRequest,
)

from stove0_core.coordinator import ObserverPort, TargetPort
from stove0_core.work_state import WorkInapplicable

NestedObservation = Callable[[WorkIdentity], tuple[ObservationEvidence, ...]]


@dataclass(slots=True)
class _PlanningFrame:
    work: WorkIdentity
    recipe: RecipeDefinition
    evidence: tuple[ObservationEvidence, ...]
    selected: tuple[tuple[RecipeBranch, ArtifactSelection], ...]
    decision_sha256: str
    root: bool
    next_branch: int = 0
    branches: list[BranchDeclaration] = field(default_factory=list)
    selections: dict[str, ArtifactSelection] = field(default_factory=dict)
    branch_sets: dict[str, BranchSetPlan] = field(default_factory=dict)
    parent_route: RecipeCoordinationRoute | None = None
    parent_selection: ArtifactSelection | None = None


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
        if isinstance(work.fork_join, JoinWorkBinding):
            return ()
        recipe = self._recipe(work)
        requests: list[ObservationRequest] = []
        inventory = self._inventory(work)
        for use in recipe.observers:
            descriptor = self.observers.descriptor(use.registration_id)
            support = descriptor.support_for(use.contract_id)
            if support.contract_sha256 != use.contract_sha256:
                raise RuntimeError("observer supports another revision of the recipe contract")
            subjects = _subjects(inventory, use.artifact_rules)
            if not subjects:
                continue
            batch_size = support.preferred_subject_batch_size
            for offset in range(0, len(subjects), batch_size):
                # Batching is an implementation preference for operational
                # efficiency, never a request, collection, or workflow limit.
                batch = subjects[offset : offset + batch_size]
                requests.append(
                    ObservationRequest.seal(
                        ObservationRequestPayload(
                            work_id=work.work_id,
                            observer_registration_id=use.registration_id,
                            observer_descriptor_sha256=descriptor.descriptor_sha256,
                            observer_contract_id=support.contract_id,
                            observer_contract_sha256=support.contract_sha256,
                            subjects=batch,
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
        *,
        nested_observer: NestedObservation | None = None,
    ) -> BranchSetDecision | WorkInapplicable:
        if isinstance(work.fork_join, JoinWorkBinding):
            raise RuntimeError("join work cannot become a coordination parent")
        prepared = self._planning_frame(work, observations, root=True)
        if isinstance(prepared, WorkInapplicable):
            return prepared

        stack = [prepared]
        completed: BranchSetDecision | None = None
        while stack:
            frame = stack[-1]
            if frame.next_branch < len(frame.selected):
                route, selection = frame.selected[frame.next_branch]
                frame.next_branch += 1
                if isinstance(route, RecipeRoute):
                    frame.branches.append(
                        self._branch_plan(
                            parent=frame.work,
                            observations=frame.evidence,
                            route=route,
                            selection=selection,
                            decision_sha256=frame.decision_sha256,
                            recipe=frame.recipe,
                        )
                    )
                    continue
                if nested_observer is None:
                    raise RuntimeError("nested coordination requires an observation authority")
                compiled_intent, _compiled_options = self._project_operation(
                    frame.work,
                    route.projections,
                )
                child_work = CoordinationBranchPlan.build_work(
                    parent_work=frame.work,
                    branch_id=route.id,
                    decision_sha256=frame.decision_sha256,
                    selection=selection,
                    recipe=route.recipe,
                    effective_intent={**route.intent, **compiled_intent},
                )
                child = self._planning_frame(
                    child_work,
                    nested_observer(child_work),
                    root=False,
                )
                if isinstance(child, WorkInapplicable):
                    return WorkInapplicable(
                        code=child.code,
                        message=f"Subrecipe branch {route.id}: {child.message}"[:1000],
                    )
                child.parent_route = route
                child.parent_selection = selection
                stack.append(child)
                continue

            join = (
                self._join_declaration(frame.work, frame.recipe.join)
                if frame.recipe.join is not None
                else None
            )
            policy = frame.recipe.source_retirement_policy if frame.root else "retain"
            plan = BranchSetPlan.seal(
                parent_work=frame.work,
                decision_sha256=frame.decision_sha256,
                evidence_sha256s=tuple(
                    sorted(item.result.result_sha256 for item in frame.evidence)
                ),
                branches=frame.branches,
                join=join,
                retirement_policy=policy,
                retirement_grace_seconds=(
                    frame.recipe.retirement_grace_seconds if frame.root else 0
                ),
                selections=frame.selections,
                branch_sets=frame.branch_sets,
            )
            completed = BranchSetDecision(
                plan=plan,
                selections=tuple(frame.selections[key] for key in sorted(frame.selections)),
                branch_sets=tuple(frame.branch_sets[key] for key in sorted(frame.branch_sets)),
            )
            stack.pop()
            if not stack:
                break

            parent = stack[-1]
            parent_route = frame.parent_route
            parent_selection = frame.parent_selection
            if parent_route is None or parent_selection is None:
                raise RuntimeError("nested planning frame lost its parent binding")
            parent.branches.append(
                CoordinationBranchPlan(
                    branch_id=parent_route.id,
                    artifact_selection=parent_selection.ref(),
                    work=frame.work,
                    branch_set_sha256=plan.branch_set_sha256,
                )
            )
            for digest, selection_document in completed.selection_documents.items():
                _retain_exact(parent.selections, digest, selection_document)
            for digest, child_plan in completed.branch_set_documents.items():
                _retain_exact(parent.branch_sets, digest, child_plan)

        if completed is None:
            raise RuntimeError("workflow planning produced no branch-set decision")
        if completed.plan.retirement_policy == "retire-after-verified-output":
            for branch in completed.leaf_branches():
                selection = completed.selection_documents[
                    branch.artifact_selection.selection_sha256
                ]
                problem = _operation_input_problem(
                    self.catalog.operation(branch.workflow_plan.operation.id),
                    selection,
                )
                if problem is not None:
                    return WorkInapplicable(
                        code="unsafe-retirement-operation-inputs",
                        message=(
                            f"Unsafe retirement inputs for branch {branch.branch_id}: {problem}"
                        ),
                    )
        return completed

    def _planning_frame(
        self,
        work: WorkIdentity,
        observations: tuple[ObservationEvidence, ...],
        *,
        root: bool,
    ) -> _PlanningFrame | WorkInapplicable:
        recipe = self._recipe(work)
        inventory = self._inventory(work)
        evidence = tuple(sorted(observations, key=lambda item: item.request.request_id))
        selected: list[tuple[RecipeBranch, ArtifactSelection]] = []
        for route in recipe.routes:
            artifacts = _route_artifacts(
                _subjects(inventory, route.artifact_rules),
                route=route,
                associations=recipe.artifact_associations,
                observations=evidence,
            )
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

        uncovered = _uncovered_inventory(inventory, selected)
        if uncovered and recipe.unmatched_artifact_disposition == "reject-work":
            return WorkInapplicable(
                code="unmatched-artifacts",
                message=(
                    "The recipe explicitly rejects work with unmatched immutable artifacts: "
                    + ", ".join(uncovered[:10])
                ),
            )

        if root and recipe.source_retirement_policy == "retire-after-verified-output":
            if uncovered:
                return WorkInapplicable(
                    code="unsafe-retirement-coverage",
                    message=(
                        "Source retirement requires the selected branch artifacts to cover "
                        "the complete immutable input inventory: " + ", ".join(uncovered[:10])
                    ),
                )
        decision_sha256 = canonical_json_sha256(
            {
                "format": "stove0-routing-decision/v1",
                "parent_work_id": work.work_id,
                "recipe": recipe.ref.model_dump(mode="json"),
                "evidence_sha256s": sorted(item.result.result_sha256 for item in evidence),
                "branches": [
                    {
                        "branch_id": route.id,
                        "kind": route.kind,
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
        documents = {selection.selection_sha256: selection for _route, selection in selected}
        return _PlanningFrame(
            work=work,
            recipe=recipe,
            evidence=evidence,
            selected=tuple(selected),
            decision_sha256=decision_sha256,
            root=root,
            selections=documents,
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
        if support.result_kind != operation.result_kind:
            raise RuntimeError("target supports another result kind for the recipe operation")
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
                result_kind=operation.result_kind,
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
        if support.result_kind != "collection" or operation.result_kind != "collection":
            raise RuntimeError("join target operation must produce a collection")
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
                result_kind="collection",
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
            protocol=self.targets.contract(plan.target_registration_id).protocol,
            operation_id=plan.operation.id,
            operation_contract_sha256=plan.operation.sha256,
            inputs=artifacts,
            intent=plan.work.effective_intent,
            target_options=plan.requested_target_options,
            observations=plan.observations,
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
                or str(current.get("content_identity") or "") != root.content_identity
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


def _route_artifacts(
    subjects: tuple[ArtifactSubject, ...],
    *,
    route: RecipeBranch,
    associations: tuple[ArtifactAssociation, ...],
    observations: tuple[ObservationEvidence, ...],
) -> tuple[ArtifactSubject, ...]:
    if route.primary_role is None:
        if all(
            _predicate_matches(predicate, observations, candidate=()) for predicate in route.when
        ):
            return subjects
        return ()

    association = next(
        (item for item in associations if item.primary_role == route.primary_role),
        None,
    )
    if route.associated_roles and association is None:
        raise RuntimeError("validated recipe route is missing its artifact association")
    primaries = [subject for subject in subjects if subject.role == route.primary_role]
    associated = [subject for subject in subjects if subject.role in set(route.associated_roles)]
    selected: dict[str, ArtifactSubject] = {}
    for primary in primaries:
        candidate = [primary]
        if route.associated_roles:
            identity = _path_association_identity(primary.path)
            candidate.extend(
                subject
                for subject in associated
                if _path_association_identity(subject.path) == identity
            )
        exact_candidate = tuple(sorted(candidate, key=lambda subject: subject.id))
        if not all(
            _predicate_matches(predicate, observations, candidate=exact_candidate)
            for predicate in route.when
        ):
            continue
        selected.update((subject.id, subject) for subject in exact_candidate)
    return tuple(selected[artifact_id] for artifact_id in sorted(selected))


def _path_association_identity(path: str) -> tuple[str, str]:
    value = PurePosixPath(path)
    return value.parent.as_posix(), value.stem


def _uncovered_inventory(
    inventory: Sequence[Mapping[str, object]],
    selected: Sequence[tuple[RecipeBranch, ArtifactSelection]],
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


def _retain_exact[T](documents: dict[str, T], digest: str, document: T) -> None:
    existing = documents.setdefault(digest, document)
    if existing != document:
        raise RuntimeError("content-addressed planning identity was reused")


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
    *,
    candidate: Sequence[ArtifactSubject],
) -> bool:
    matches = [
        evidence.result
        for evidence in observations
        if evidence.request.observer_contract_id == predicate.observation_contract_id
    ]
    if not matches or any(result.state != "observed" or result.facts is None for result in matches):
        return False
    if predicate.artifact_roles:
        relevant = {
            artifact.id for artifact in candidate if artifact.role in set(predicate.artifact_roles)
        }
        if not relevant:
            return predicate.operator == "exists" and predicate.value is False
        assert predicate.artifact_facts is not None
        records = _artifact_fact_records(matches, predicate.artifact_facts, relevant)
        if set(records) != relevant:
            return False
        values = [record for artifact_id in sorted(records) for record in records[artifact_id]]
        if predicate.operator == "exists" and predicate.value is False:
            return all(not _json_pointer(record, predicate.pointer)[0] for record in values)
        return any(_document_matches_predicate(predicate, record) for record in values)
    if predicate.operator == "exists" and predicate.value is False:
        return all(not _json_pointer(result.facts, predicate.pointer)[0] for result in matches)
    return any(
        result.facts is not None and _document_matches_predicate(predicate, result.facts)
        for result in matches
    )


def _artifact_fact_records(
    results: Sequence[ObservationResult],
    binding: ArtifactFactBinding,
    artifact_ids: set[str],
) -> dict[str, list[dict[str, JsonValue]]]:
    records: dict[str, list[dict[str, JsonValue]]] = {}
    for result in results:
        assert result.facts is not None
        present, raw_records = _json_pointer(result.facts, binding.records_pointer)
        if not present or not isinstance(raw_records, list):
            continue
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            has_id, artifact_id = _json_pointer(raw_record, binding.artifact_id_pointer)
            if not has_id or not isinstance(artifact_id, str) or artifact_id not in artifact_ids:
                continue
            records.setdefault(artifact_id, []).append(raw_record)
    return records


def _document_matches_predicate(
    predicate: FactPredicate,
    document: dict[str, JsonValue],
) -> bool:
    present, value = _json_pointer(document, predicate.pointer)
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
    "ArtifactAssociation",
    "ArtifactFactBinding",
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
