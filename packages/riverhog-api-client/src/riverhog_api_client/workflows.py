"""Official client methods for Riverhog collection work primitives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import quote


class CollectionWorkflowMethods:
    """Mixin exposing Riverhog's generic collection-work API on ``ApiClient``."""

    if TYPE_CHECKING:

        def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...

    def create_or_resume_processing_claim(
        self,
        *,
        work_id: str,
        work_document: Mapping[str, Any],
        work_document_sha256: str,
        inputs: Sequence[Mapping[str, Any]],
        lease_seconds: int = 1800,
        purpose: str = "collection-work/v1",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/collection-processing-claims",
            json={
                "work_id": work_id,
                "work_document": dict(work_document),
                "work_document_sha256": work_document_sha256,
                "inputs": [dict(item) for item in inputs],
                "lease_seconds": lease_seconds,
                "purpose": purpose,
            },
        )

    def get_processing_claim(self, claim_id: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}",
        )

    def list_processing_claims(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        state: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        all_items: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if state:
            params["state"] = state
        if all_items:
            params["all"] = True
        return self._json("GET", "/v1/collection-processing-claims", params=params)

    def renew_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        lease_seconds: int = 1800,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/renew",
            json={"fence": fence, "lease_seconds": lease_seconds},
        )

    def restart_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        lease_seconds: int = 1800,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/restart",
            json={"fence": fence, "lease_seconds": lease_seconds},
        )

    def abandon_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        reason: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/abandon",
            json={"fence": fence, "reason": reason},
        )

    def seal_processing_claim_plan(
        self,
        claim_id: str,
        *,
        fence: int,
        execution_id: str,
        controller_evidence: Mapping[str, Any],
        controller_evidence_sha256: str,
        operation_id: str,
        operation_sha256: str,
        input_artifacts: Sequence[Mapping[str, Any]],
        output_tags: Sequence[str],
        retirement_policy: str = "retain",
        retirement_grace_seconds: int = 0,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/plan",
            json={
                "fence": fence,
                "execution_id": execution_id,
                "controller_evidence": dict(controller_evidence),
                "controller_evidence_sha256": controller_evidence_sha256,
                "operation": {"id": operation_id, "sha256": operation_sha256},
                "input_artifacts": [dict(item) for item in input_artifacts],
                "output_tags": list(output_tags),
                "retirement_policy": retirement_policy,
                "retirement_grace_seconds": retirement_grace_seconds,
            },
        )

    def create_transform_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        audience: str,
        actions: Sequence[str] = ("read-inputs",),
        artifacts: Sequence[Mapping[str, Any]],
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/capabilities",
            json={
                "fence": fence,
                "audience": audience,
                "actions": list(actions),
                "artifacts": [dict(item) for item in artifacts],
                "ttl_seconds": ttl_seconds,
            },
        )

    def settle_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, Any],
        outcome_claim_id: str | None = None,
        outcome_fence: int | None = None,
        outcome_id: str | None = None,
    ) -> dict[str, Any]:
        outcome = None
        if any(item is not None for item in (outcome_claim_id, outcome_fence, outcome_id)):
            if outcome_claim_id is None or outcome_fence is None or outcome_id is None:
                raise ValueError("processing outcome binding is incomplete")
            outcome = {
                "claim_id": outcome_claim_id,
                "fence": outcome_fence,
                "outcome_id": outcome_id,
            }
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/settle",
            json={
                "fence": fence,
                "output_collection_id": output_collection_id,
                "derivation": dict(derivation),
                **({"outcome": outcome} if outcome is not None else {}),
            },
        )

    def settle_processing_claim_outcomes(
        self,
        claim_id: str,
        *,
        fence: int,
        outcomes: Sequence[Mapping[str, Any]],
        retirement_policy: str = "retain",
        retirement_grace_seconds: int = 0,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/outcomes/settle",
            json={
                "fence": fence,
                "outcomes": [dict(item) for item in outcomes],
                "retirement_policy": retirement_policy,
                "retirement_grace_seconds": retirement_grace_seconds,
            },
        )

    def begin_processing_claim_retirement(
        self,
        claim_id: str,
        *,
        fence: int,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/retirement",
            json={"fence": fence},
        )

    def release_processing_claim(
        self,
        claim_id: str,
        *,
        fence: int,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/release",
            json={"fence": fence},
        )

    def get_collection_derivation(self, collection_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v1/collections/{collection_id}/derivation")


__all__ = ["CollectionWorkflowMethods"]
