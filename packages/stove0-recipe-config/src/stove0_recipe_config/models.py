"""Portable, deployment-owned Stove0 recipe catalog contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from config_validation import load_yaml_config
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from stove0_protocol import RecipeRef, SemanticId, Sha256, canonical_json_sha256
from stove0_target_protocol import OperationContract

_JSON_POINTER_PATTERN = r"^(?:|/(?:[^~/]|~[01])*(?:/(?:[^~/]|~[01])*)*)$"


class RecipeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRule(RecipeModel):
    """Classify one path; first matching rule wins."""

    glob: str = "*"
    role: SemanticId = "stove0.source/v1"
    media_type: str | None = None


class ArtifactAssociation(RecipeModel):
    """Associate classified artifacts without assigning device meaning to Stove0."""

    primary_role: SemanticId
    associated_roles: tuple[SemanticId, ...] = Field(min_length=1)
    path_identity: Literal["same-parent-stem"] = "same-parent-stem"

    @field_validator("associated_roles")
    @classmethod
    def canonical_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("associated artifact roles must be unique and canonical")
        return value


class ObserverUse(RecipeModel):
    registration_id: str
    contract_id: SemanticId
    contract_sha256: Sha256
    artifact_rules: tuple[ArtifactRule, ...] = (ArtifactRule(),)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    maximum_result_bytes: int = Field(default=1024 * 1024, ge=1, le=64 * 1024 * 1024)
    retrieval_policy: Literal["available-only", "allow"] = "available-only"


class ArtifactFactBinding(RecipeModel):
    """Locate subject-keyed records inside one observer's declared facts schema."""

    records_pointer: str = Field(pattern=_JSON_POINTER_PATTERN)
    artifact_id_pointer: str = Field(default="/artifact_id", pattern=_JSON_POINTER_PATTERN)


class FactPredicate(RecipeModel):
    observation_contract_id: SemanticId
    artifact_roles: tuple[SemanticId, ...] = ()
    artifact_facts: ArtifactFactBinding | None = None
    pointer: str = Field(pattern=_JSON_POINTER_PATTERN)
    operator: Literal["equals", "not-equals", "contains", "exists"] = "equals"
    value: JsonValue = None

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        if self.artifact_roles != tuple(sorted(set(self.artifact_roles))):
            raise ValueError("predicate artifact roles must be unique and canonical")
        if bool(self.artifact_roles) != (self.artifact_facts is not None):
            raise ValueError(
                "artifact-scoped predicates require artifact roles and an artifact-facts binding"
            )
        if self.operator == "exists" and not isinstance(self.value, bool):
            raise ValueError("exists predicates require a boolean value")
        return self


class OperationProjection(RecipeModel):
    """One declarative JSON-pointer copy into an operation request."""

    source: Literal["work-effective-intent", "work-evaluation"]
    source_pointer: str = Field(pattern=_JSON_POINTER_PATTERN)
    destination: Literal["intent", "target-options"]
    destination_pointer: str = Field(pattern=_JSON_POINTER_PATTERN)


class _RecipeRouteBase(RecipeModel):
    id: SemanticId
    when: tuple[FactPredicate, ...] = ()
    artifact_rules: tuple[ArtifactRule, ...] = (ArtifactRule(),)
    primary_role: SemanticId | None = None
    associated_roles: tuple[SemanticId, ...] = ()
    intent: dict[str, JsonValue] = Field(default_factory=dict)
    projections: tuple[OperationProjection, ...] = ()

    @model_validator(mode="after")
    def canonical_members(self) -> Self:
        _validate_projections(self.projections)
        if self.associated_roles != tuple(sorted(set(self.associated_roles))):
            raise ValueError("route associated roles must be unique and canonical")
        if self.associated_roles and self.primary_role is None:
            raise ValueError("associated roles require per-artifact routing with a primary role")
        candidate_roles = set(self.associated_roles)
        if self.primary_role is not None:
            candidate_roles.add(self.primary_role)
        scoped_roles = {role for predicate in self.when for role in predicate.artifact_roles}
        unknown = sorted(scoped_roles - candidate_roles)
        if unknown:
            raise ValueError(
                "artifact-scoped predicates reference roles outside the route candidate: "
                + ", ".join(unknown)
            )
        if scoped_roles and self.primary_role is None:
            raise ValueError("artifact-scoped predicates require a route primary role")
        return self


class RecipeRoute(_RecipeRouteBase):
    """One ordinary target/effect leaf selected by a recipe."""

    kind: Literal["operation"] = "operation"
    operation_id: SemanticId
    target_registration_id: str
    target_options: dict[str, JsonValue] = Field(default_factory=dict)
    input_retrieval_policy: Literal["available-only", "allow"] = "available-only"
    output_tags: tuple[str, ...] = ()


class RecipeCoordinationRoute(_RecipeRouteBase):
    """One exact subrecipe selected as a branch-bound coordinator."""

    kind: Literal["coordination"] = "coordination"
    recipe: RecipeRef

    @model_validator(mode="after")
    def coordination_projections_target_intent_only(self) -> Self:
        if any(item.destination == "target-options" for item in self.projections):
            raise ValueError("coordination routes may project only child effective intent")
        return self


RecipeBranch = Annotated[
    RecipeRoute | RecipeCoordinationRoute,
    Field(discriminator="kind"),
]


class RecipeJoinMember(RecipeModel):
    branch_id: SemanticId
    output_roles: tuple[SemanticId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_roles(self) -> Self:
        if self.output_roles != tuple(sorted(set(self.output_roles))):
            raise ValueError("join output roles must be unique and canonical")
        return self


class RecipeJoin(RecipeModel):
    id: SemanticId
    members: tuple[RecipeJoinMember, ...] = Field(min_length=2)
    operation_id: SemanticId
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
    id: SemanticId
    revision: int = Field(ge=1)
    input_tags: tuple[str, ...] = Field(min_length=1)
    event_input_closure: Literal["single-finalized-collection"] = "single-finalized-collection"
    artifact_associations: tuple[ArtifactAssociation, ...] = ()
    observers: tuple[ObserverUse, ...] = ()
    routes: tuple[RecipeBranch, ...] = Field(min_length=1)
    unmatched_artifact_disposition: Literal["retain-in-source", "reject-work"]
    allow_derived_inputs: bool = False
    source_retirement_policy: Literal["retain", "retire-after-verified-output"] = "retain"
    retirement_grace_seconds: int = Field(default=0, ge=0)
    join: RecipeJoin | None = None

    @model_validator(mode="after")
    def canonical_members(self) -> Self:
        if self.input_tags != tuple(sorted(set(self.input_tags))):
            raise ValueError("recipe input tags must be unique and canonical")
        association_roles = [item.primary_role for item in self.artifact_associations]
        if association_roles != sorted(association_roles) or len(association_roles) != len(
            set(association_roles)
        ):
            raise ValueError("artifact associations must be unique and ordered by primary role")
        associations = {item.primary_role: item for item in self.artifact_associations}
        for route in self.routes:
            if not route.associated_roles:
                continue
            assert route.primary_role is not None
            association = associations.get(route.primary_role)
            if association is None:
                raise ValueError(f"route {route.id} has no artifact association")
            unknown_roles = sorted(set(route.associated_roles) - set(association.associated_roles))
            if unknown_roles:
                raise ValueError(
                    f"route {route.id} references undeclared associated roles: "
                    + ", ".join(unknown_roles)
                )
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

    def identity_document(self) -> dict[str, JsonValue]:
        return {"id": self.id, "revision": self.revision, "sha256": self.sha256}


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
        recipes = {(recipe.id, recipe.revision): recipe for recipe in self.recipes}
        for recipe in self.recipes:
            for route in recipe.routes:
                if not isinstance(route, RecipeCoordinationRoute):
                    continue
                child = recipes.get((route.recipe.id, route.recipe.revision))
                if child is None or child.sha256 != route.recipe.sha256:
                    raise ValueError(
                        f"recipe {recipe.id} references unavailable exact subrecipe "
                        f"{route.recipe.id}@{route.recipe.revision}"
                    )
        _validate_recipe_cycles(self.recipes, recipes)
        for recipe in self.recipes:
            referenced = [
                route.operation_id for route in recipe.routes if isinstance(route, RecipeRoute)
            ]
            if recipe.join is not None:
                referenced.append(recipe.join.operation_id)
            unknown = sorted(set(referenced) - set(operations))
            if unknown:
                raise ValueError(
                    f"recipe {recipe.id} references unknown operation(s): " + ", ".join(unknown)
                )
            for route in recipe.routes:
                if isinstance(route, RecipeCoordinationRoute):
                    continue
                operation = operations[route.operation_id]
                if operation.result_kind == "collection" and not route.output_tags:
                    raise ValueError(
                        f"recipe {recipe.id} collection branch {route.id} requires output tags"
                    )
                if operation.result_kind == "external-effect" and route.output_tags:
                    raise ValueError(
                        f"recipe {recipe.id} effect branch {route.id} cannot declare output tags"
                    )
            if recipe.join is not None:
                join_operation = operations[recipe.join.operation_id]
                if join_operation.result_kind != "collection":
                    raise ValueError(f"recipe {recipe.id} join must produce a collection")
                route_result_kinds: dict[str, str] = {}
                for route in recipe.routes:
                    if isinstance(route, RecipeRoute):
                        route_result_kinds[route.id] = operations[route.operation_id].result_kind
                        continue
                    child = recipes[(route.recipe.id, route.recipe.revision)]
                    route_result_kinds[route.id] = (
                        "collection" if child.join is not None else "coordination"
                    )
                effect_members = [
                    member.branch_id
                    for member in recipe.join.members
                    if route_result_kinds[member.branch_id] != "collection"
                ]
                if effect_members:
                    raise ValueError(
                        f"recipe {recipe.id} join cannot consume non-collection branch(es): "
                        + ", ".join(effect_members)
                    )
            if recipe.source_retirement_policy == "retire-after-verified-output":
                unsafe: list[str] = []
                for route in recipe.routes:
                    if isinstance(route, RecipeRoute):
                        if not operations[route.operation_id].source_retirement_permitted:
                            unsafe.append(route.id)
                        continue
                    child = recipes[(route.recipe.id, route.recipe.revision)]
                    if any(
                        not operations[operation_id].source_retirement_permitted
                        for operation_id in _descendant_operation_ids(child, recipes)
                    ):
                        unsafe.append(route.id)
                unsafe.sort()
                if unsafe:
                    raise ValueError(
                        f"recipe {recipe.id} retires its source but branch operation(s) do not "
                        "authorize retirement: " + ", ".join(unsafe)
                    )
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json", by_alias=True, exclude_none=True))

    def operation(self, operation_id: str) -> OperationContract:
        for operation in self.operations:
            if operation.id == operation_id:
                return operation
        raise KeyError(operation_id)

    @classmethod
    def load(cls, path: Path) -> RecipeCatalog:
        return cls.model_validate(load_yaml_config(Path(path)))

    def recipe(self, recipe_id: str, revision: int | None = None) -> RecipeDefinition:
        matches = [
            recipe
            for recipe in self.recipes
            if recipe.id == recipe_id and (revision is None or recipe.revision == revision)
        ]
        if not matches:
            raise KeyError(recipe_id)
        return max(matches, key=lambda recipe: recipe.revision)

    def matching(self, tags: tuple[str, ...] | list[str]) -> tuple[RecipeDefinition, ...]:
        available = set(tags)
        return tuple(
            recipe for recipe in self.recipes if set(recipe.input_tags).issubset(available)
        )

    def validation_document(self) -> dict[str, JsonValue]:
        return {
            "format": "stove0-recipe-catalog-validation/v1",
            "catalog_sha256": self.sha256,
            "operation_count": len(self.operations),
            "recipe_count": len(self.recipes),
            "recipes": [recipe.identity_document() for recipe in self.recipes],
        }


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


def _validate_recipe_cycles(
    recipes: tuple[RecipeDefinition, ...],
    by_identity: dict[tuple[str, int], RecipeDefinition],
) -> None:
    """Reject exact subrecipe cycles without imposing a depth ceiling."""

    complete: set[tuple[str, int]] = set()
    for root in ((item.id, item.revision) for item in recipes):
        if root in complete:
            continue
        visiting: set[tuple[str, int]] = set()
        stack: list[tuple[tuple[str, int], bool]] = [(root, False)]
        while stack:
            identity, leaving = stack.pop()
            if leaving:
                visiting.remove(identity)
                complete.add(identity)
                continue
            if identity in complete:
                continue
            if identity in visiting:
                raise ValueError("recipe catalog contains a subrecipe cycle")
            visiting.add(identity)
            stack.append((identity, True))
            recipe = by_identity[identity]
            children = [
                (route.recipe.id, route.recipe.revision)
                for route in recipe.routes
                if isinstance(route, RecipeCoordinationRoute)
            ]
            for child in reversed(children):
                if child in visiting:
                    raise ValueError("recipe catalog contains a subrecipe cycle")
                if child not in complete:
                    stack.append((child, False))


def _descendant_operation_ids(
    recipe: RecipeDefinition,
    by_identity: dict[tuple[str, int], RecipeDefinition],
) -> tuple[str, ...]:
    operations: set[str] = set()
    stack = [recipe]
    while stack:
        current = stack.pop()
        for route in current.routes:
            if isinstance(route, RecipeRoute):
                operations.add(route.operation_id)
            else:
                stack.append(by_identity[(route.recipe.id, route.recipe.revision)])
        if current.join is not None:
            operations.add(current.join.operation_id)
    return tuple(sorted(operations))


__all__ = [
    "ArtifactAssociation",
    "ArtifactFactBinding",
    "ArtifactRule",
    "FactPredicate",
    "ObserverUse",
    "OperationProjection",
    "RecipeBranch",
    "RecipeCatalog",
    "RecipeCoordinationRoute",
    "RecipeDefinition",
    "RecipeJoin",
    "RecipeJoinMember",
    "RecipeRoute",
]
