"""Official client methods for Riverhog collection workflow primitives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from riverhog_api_client.client import ApiClient


class CollectionWorkflowClient(ApiClient):
    def create_or_resume_processing_claim(
        self,
        *,
        input_collection_ids: Sequence[int],
        recipe_id: str,
        recipe_revision: int,
        recipe_sha256: str,
        operation_id: str,
        operation_sha256: str,
        effective_intent: Mapping[str, Any],
        output_tags: Sequence[str],
        retirement_policy: str = "retain",
        retirement_grace_seconds: int = 0,
        lease_seconds: int = 1800,
        purpose: str = "collection-transform/v1",
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v1/collection-processing-claims",
            json={
                "input_collection_ids": list(input_collection_ids),
                "recipe": {
                    "id": recipe_id,
                    "revision": recipe_revision,
                    "sha256": recipe_sha256,
                },
                "operation": {"id": operation_id, "sha256": operation_sha256},
                "effective_intent": dict(effective_intent),
                "output_tags": list(output_tags),
                "retirement_policy": retirement_policy,
                "retirement_grace_seconds": retirement_grace_seconds,
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

    def create_transform_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        actions: Sequence[str] = ("read-inputs", "write-output"),
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/capabilities",
            json={
                "fence": fence,
                "actions": list(actions),
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
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/collection-processing-claims/{quote(claim_id, safe='')}/settle",
            json={
                "fence": fence,
                "output_collection_id": output_collection_id,
                "derivation": dict(derivation),
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
