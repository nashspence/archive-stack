"""Payload-free, tag-targeted Riverhog collection transformation control.

Jeb owns selection, deterministic intent, claim renewal, verification, and
optional retirement. It never reads collection payloads and never trusts a
Munchy success response without verifying the finalized Riverhog output.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from riverhog_protocol.collection_workflows import (
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    RetirementPolicy,
    TransformIntent,
    canonical_json_sha256,
)


class RiverhogCollectionWorkflowApi(Protocol):
    def list_collections(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        q: str | None = None,
        tag: str | None = None,
        sort: str = "id",
        order: str = "asc",
        all_items: bool = False,
    ) -> dict[str, Any]: ...

    def get_collection(self, collection_id: int) -> dict[str, Any]: ...

    def create_or_resume_processing_claim(self, **request: Any) -> dict[str, Any]: ...

    def renew_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        lease_seconds: int,
    ) -> dict[str, Any]: ...

    def create_transform_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        actions: Sequence[str],
        ttl_seconds: int,
    ) -> dict[str, Any]: ...

    def settle_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, object],
    ) -> dict[str, Any]: ...

    def get_processing_claim(self, claim_id: str) -> dict[str, Any]: ...

    def begin_processing_claim_retirement(
        self,
        claim_id: str,
        *,
        fence: int,
    ) -> dict[str, Any]: ...

    def release_processing_claim(self, claim_id: str, *, fence: int) -> dict[str, Any]: ...

    def get_collection_derivation(self, collection_id: int) -> dict[str, Any]: ...

    def plan_collection_deletion(
        self,
        collection_id: int,
        *,
        retirement_claim_id: str | None = None,
    ) -> dict[str, Any]: ...

    def delete_collection(
        self,
        collection_id: int,
        *,
        challenge: str,
        retirement_claim_id: str | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class MunchyCollectionTransformApi(Protocol):
    def create_or_resume_collection_transform(
        self,
        *,
        job_id: str,
        claim_id: str,
        fence: int,
        capability_token: str,
        intent: Mapping[str, object],
    ) -> dict[str, Any]: ...

    def get_collection_transform(self, job_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CollectionBatchPolicy:
    max_collections: int = 32
    max_bytes: int = 256 * 1024**3

    def __post_init__(self) -> None:
        if self.max_collections < 1 or self.max_bytes < 1:
            raise ValueError("collection batch limits must be positive")


@dataclass(frozen=True, slots=True)
class CollectionTransformRecipe:
    id: str
    revision: int
    operation_id: str
    operation_sha256: str
    select_all_tags: tuple[str, ...]
    output_tags: tuple[str, ...]
    effective_intent: dict[str, object]
    select_none_tags: tuple[str, ...] = ()
    retirement_policy: RetirementPolicy = "retain"
    retirement_grace_seconds: int = 0
    batch: CollectionBatchPolicy = CollectionBatchPolicy()
    purpose: str = "collection-transform/v1"

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(
            {
                "id": self.id,
                "revision": self.revision,
                "select": {
                    "all_tags": sorted(self.select_all_tags),
                    "none_tags": sorted(self.select_none_tags),
                },
                "operation": {
                    "id": self.operation_id,
                    "sha256": self.operation_sha256,
                },
                "intent": self.effective_intent,
                "output_tags": sorted(self.output_tags),
                "retirement": {
                    "policy": self.retirement_policy,
                    "grace_seconds": self.retirement_grace_seconds,
                },
                "batch": {
                    "max_collections": self.batch.max_collections,
                    "max_bytes": self.batch.max_bytes,
                },
            }
        )


@dataclass(frozen=True, slots=True)
class TransformWork:
    recipe: CollectionTransformRecipe
    intent: TransformIntent


TransformOutcome = Literal["settled", "failed", "active", "released"]


class JebCollectionController:
    """Coordinate collection transformations without accepting payload custody."""

    def __init__(
        self,
        riverhog: RiverhogCollectionWorkflowApi,
        munchy: MunchyCollectionTransformApi,
        *,
        claim_lease_seconds: int = 30 * 60,
        capability_ttl_seconds: int = 30 * 60,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.riverhog = riverhog
        self.munchy = munchy
        self.claim_lease_seconds = claim_lease_seconds
        self.capability_ttl_seconds = capability_ttl_seconds
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def plan(self, recipe: CollectionTransformRecipe) -> list[TransformWork]:
        if not recipe.select_all_tags:
            raise ValueError("Jeb collection recipes require at least one selector tag")
        page = self.riverhog.list_collections(
            tag=recipe.select_all_tags[0],
            all_items=True,
            sort="id",
            order="asc",
        )
        values = page.get("collections")
        if not isinstance(values, list):
            raise RuntimeError("Riverhog collection list did not return collections")
        candidates: list[tuple[CollectionRootIdentity, int]] = []
        required = set(recipe.select_all_tags)
        excluded = set(recipe.select_none_tags)
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            tags = {str(value) for value in raw.get("tags") or []}
            if not required.issubset(tags) or tags.intersection(excluded):
                continue
            root = _root_from_collection(raw)
            candidates.append((root, int(raw.get("bytes") or 0)))
        return [
            TransformWork(recipe=recipe, intent=self._intent(recipe, roots))
            for roots in _bounded_batches(candidates, recipe.batch)
        ]

    def reconcile(
        self,
        work: TransformWork,
        *,
        retire_inputs: bool = False,
    ) -> dict[str, object]:
        intent = work.intent
        claim = self.riverhog.create_or_resume_processing_claim(
            input_collection_ids=[item.collection_id for item in intent.inputs],
            recipe_id=intent.recipe.id,
            recipe_revision=intent.recipe.revision,
            recipe_sha256=intent.recipe.sha256,
            operation_id=intent.operation.id,
            operation_sha256=intent.operation.sha256,
            effective_intent=intent.effective_intent,
            output_tags=list(intent.output_tags),
            retirement_policy=intent.retirement_policy,
            retirement_grace_seconds=intent.retirement_grace_seconds,
            lease_seconds=self.claim_lease_seconds,
            purpose=work.recipe.purpose,
        )
        state = str(claim.get("state") or "")
        if state in {"settled", "retiring", "released"}:
            self.verify_settled_claim(claim, intent)
            if retire_inputs and state != "released":
                return self.retire_inputs(claim, intent)
            if state == "settled" and intent.retirement_policy == "retain":
                return self.riverhog.release_processing_claim(
                    str(claim["id"]), fence=int(claim["fence"])
                )
            return claim
        if state != "active":
            raise RuntimeError(f"Riverhog returned unsupported claim state: {state}")
        fence = int(claim["fence"])
        capability = self.riverhog.create_transform_capability(
            str(claim["id"]),
            fence=fence,
            actions=("read-inputs", "write-output"),
            ttl_seconds=self.capability_ttl_seconds,
        )
        job = self.munchy.create_or_resume_collection_transform(
            job_id=intent.transform_id,
            claim_id=str(claim["id"]),
            fence=fence,
            capability_token=str(capability["token"]),
            intent=intent.as_dict(),
        )
        deadline = time.monotonic() + self.timeout_seconds
        renewed_at = time.monotonic()
        while str(job.get("state") or "") not in {"succeeded", "failed", "canceled"}:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"Munchy transform {intent.transform_id} did not settle")
            if now - renewed_at >= max(10.0, self.claim_lease_seconds / 3):
                claim = self.riverhog.renew_processing_claim(
                    str(claim["id"]),
                    fence=fence,
                    lease_seconds=self.claim_lease_seconds,
                )
                renewed_at = now
            time.sleep(max(0.05, self.poll_seconds))
            job = self.munchy.get_collection_transform(intent.transform_id)
        if str(job.get("state")) != "succeeded":
            return {
                "status": "failed",
                "claim": claim,
                "job": job,
            }
        output_collection_id = int(job["output_collection_id"])
        derivation = job.get("derivation")
        if not isinstance(derivation, Mapping):
            raise RuntimeError("Munchy success has no immutable derivation document")
        settled = self.riverhog.settle_processing_claim(
            str(claim["id"]),
            fence=fence,
            output_collection_id=output_collection_id,
            derivation=derivation,
        )
        self.verify_settled_claim(settled, intent)
        if retire_inputs:
            return self.retire_inputs(settled, intent)
        if intent.retirement_policy == "retain":
            return self.riverhog.release_processing_claim(
                str(settled["id"]), fence=int(settled["fence"])
            )
        return settled

    def verify_settled_claim(
        self,
        claim: Mapping[str, object],
        intent: TransformIntent,
    ) -> None:
        output_collection_id = claim.get("output_collection_id")
        if not isinstance(output_collection_id, int):
            raise RuntimeError("settled collection claim has no output collection")
        output = self.riverhog.get_collection(output_collection_id)
        tags = tuple(sorted(str(value) for value in output.get("tags") or []))
        if tags != intent.output_tags:
            raise RuntimeError("derived collection tags differ from the sealed transform intent")
        root = _root_from_collection(output)
        if root.collection_id != output_collection_id:
            raise RuntimeError("derived collection root identity is inconsistent")
        payload = self.riverhog.get_collection_derivation(output_collection_id)
        raw = payload.get("derivation")
        if not isinstance(raw, Mapping):
            raise RuntimeError("Riverhog derived collection has no derivation document")
        derivation = CollectionDerivation.from_mapping(raw)
        if (
            derivation.transform_id != intent.transform_id
            or derivation.claim_id != claim.get("id")
            or derivation.fence != int(claim["fence"])
            or derivation.recipe != intent.recipe
            or derivation.operation != intent.operation
            or derivation.inputs != intent.inputs
            or derivation.output_tags != intent.output_tags
        ):
            raise RuntimeError("Riverhog derivation does not prove the expected transformation")

    def retire_inputs(
        self,
        claim: Mapping[str, object],
        intent: TransformIntent,
    ) -> dict[str, object]:
        if intent.retirement_policy != "retire-after-verified-output":
            return dict(claim)
        claim_id = str(claim["id"])
        fence = int(claim["fence"])
        current = self.riverhog.begin_processing_claim_retirement(claim_id, fence=fence)
        for root in intent.inputs:
            try:
                plan = self.riverhog.plan_collection_deletion(
                    root.collection_id,
                    retirement_claim_id=claim_id,
                )
            except Exception:
                # A prior retry may already have deleted this exact input. Riverhog's
                # release check remains the authoritative absence verification.
                continue
            challenge = plan.get("challenge")
            blockers = plan.get("blockers")
            if blockers:
                raise RuntimeError(
                    f"Riverhog refused verified input retirement: {root.collection_id}: {blockers}"
                )
            if not isinstance(challenge, str) or not challenge:
                raise RuntimeError("Riverhog deletion plan returned no retirement challenge")
            self.riverhog.delete_collection(
                root.collection_id,
                challenge=challenge,
                retirement_claim_id=claim_id,
                event_context={
                    "initiator": {
                        "app": "jeb",
                        "claim_id": claim_id,
                        "transform_id": intent.transform_id,
                    }
                },
            )
        return self.riverhog.release_processing_claim(claim_id, fence=fence)

    @staticmethod
    def _intent(
        recipe: CollectionTransformRecipe,
        roots: Sequence[CollectionRootIdentity],
    ) -> TransformIntent:
        return TransformIntent.seal(
            recipe=RecipeIdentity(recipe.id, recipe.revision, recipe.sha256),
            operation=OperationIdentity(recipe.operation_id, recipe.operation_sha256),
            inputs=roots,
            effective_intent=recipe.effective_intent,
            output_tags=recipe.output_tags,
            retirement_policy=recipe.retirement_policy,
            retirement_grace_seconds=recipe.retirement_grace_seconds,
        )


def _bounded_batches(
    values: Sequence[tuple[CollectionRootIdentity, int]],
    policy: CollectionBatchPolicy,
) -> list[tuple[CollectionRootIdentity, ...]]:
    batches: list[tuple[CollectionRootIdentity, ...]] = []
    current: list[CollectionRootIdentity] = []
    current_bytes = 0
    for root, byte_count in values:
        if current and (
            len(current) >= policy.max_collections
            or current_bytes + max(0, byte_count) > policy.max_bytes
        ):
            batches.append(tuple(current))
            current = []
            current_bytes = 0
        current.append(root)
        current_bytes += max(0, byte_count)
    if current:
        batches.append(tuple(current))
    return batches


def _root_from_collection(value: Mapping[str, object]) -> CollectionRootIdentity:
    manifest_sha256 = str(value.get("manifest_sha256") or "")
    content_etag = str(value.get("content_etag") or "")
    return CollectionRootIdentity(
        collection_id=int(value["id"]),
        manifest_sha256=manifest_sha256,
        content_etag=content_etag,
    )


__all__ = [
    "CollectionBatchPolicy",
    "CollectionTransformRecipe",
    "JebCollectionController",
    "MunchyCollectionTransformApi",
    "RiverhogCollectionWorkflowApi",
    "TransformWork",
]
