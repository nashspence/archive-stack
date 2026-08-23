"""Claim-bound publication of exactly one finalized derived collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from riverhog_api_client.producer import CollectionProducer, ProducerInput, ProvenanceBuilder
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDisposition,
    CollectionDerivation,
    JsonValue,
    canonical_json_sha256,
)

from riverhog_transform_sdk.models import DerivedCollectionReceipt, DerivedCollectionSpec


def _sha256(value: str, label: str) -> str:
    normalized = value.casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return normalized


class DerivedCollectionWriter:
    """Publish one output collection through a scoped transform capability.

    The writer depends only on controller-sealed identities and evidence. It does
    not import an orchestration application, inspect contents, or choose archive
    object locations.
    """

    def __init__(
        self,
        api: Any,
        *,
        spec: DerivedCollectionSpec,
        claim_id: str,
        fence: int,
        work_id: str,
        execution_id: str,
        controller_evidence: Mapping[str, object],
        producer_app: str,
        producer_version: str = "development",
    ) -> None:
        if not claim_id or claim_id != claim_id.strip():
            raise ValueError("derived collection writer requires a canonical claim id")
        if isinstance(fence, bool) or fence < 1:
            raise ValueError("derived collection writer requires a positive fence")
        evidence = dict(controller_evidence)
        if not evidence:
            raise ValueError("derived collection writer requires controller evidence")
        self.api = api
        self.spec = spec
        self.claim_id = claim_id
        self.fence = fence
        self.work_id = _sha256(work_id, "work identity")
        self.execution_id = _sha256(execution_id, "execution identity")
        self.controller_evidence = evidence
        self.controller_evidence_sha256 = canonical_json_sha256(evidence)
        self.producer_app = producer_app
        self.producer_version = producer_version

    def replace_api(self, api: Any) -> None:
        self.api = api

    def publish(
        self,
        outputs: Sequence[ProducerInput],
        *,
        execution_envelope_sha256: str,
        execution_sha256: str,
        dispositions: Sequence[ArtifactDisposition],
        provenance_builder: ProvenanceBuilder | None = None,
        source_context: Mapping[str, object] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> DerivedCollectionReceipt:
        normalized_outputs = tuple(outputs)
        if not normalized_outputs:
            raise ValueError("successful collection transform must produce output artifacts")
        output_paths = {current.path for current in normalized_outputs}
        if len(output_paths) != len(normalized_outputs):
            raise ValueError("derived collection output paths must be unique")
        if any(
            path in {PRODUCER_EVIDENCE_PATH, DERIVATION_EVIDENCE_PATH}
            or path.startswith("riverhog/")
            for path in output_paths
        ):
            raise ValueError("transform targets may not write Riverhog control paths")
        normalized_dispositions = tuple(sorted(dispositions))
        referenced_outputs = {
            path for disposition in normalized_dispositions for path in disposition.outputs
        }
        if referenced_outputs != output_paths:
            raise ValueError(
                "derived collection outputs must be referenced exactly by artifact dispositions"
            )
        derivation = CollectionDerivation(
            execution_id=self.execution_id,
            claim_id=self.claim_id,
            fence=self.fence,
            recipe=self.spec.recipe,
            operation=self.spec.operation,
            inputs=self.spec.inputs,
            output_tags=self.spec.output_tags,
            execution_envelope_sha256=_sha256(
                execution_envelope_sha256,
                "execution envelope identity",
            ),
            execution_sha256=_sha256(execution_sha256, "execution evidence identity"),
            controller_evidence=cast(dict[str, JsonValue], self.controller_evidence),
            controller_evidence_sha256=self.controller_evidence_sha256,
            dispositions=normalized_dispositions,
        )
        producer = CollectionProducer(
            self.api,
            producer_app=self.producer_app,
            adapter_id="riverhog-derived-collection/v1",
            adapter_version=self.producer_version,
            ingest_source=f"transform:{self.execution_id}",
            tags=self.spec.output_tags,
            provenance_omission_reason=(
                "Transform output has no captured host journal for this artifact; "
                "the immutable derivation document records exact execution evidence."
            ),
        )
        receipt = producer.publish_inputs(
            normalized_outputs,
            source_event_id=self.execution_id,
            source_context={
                **dict(source_context or {}),
                "claim_id": self.claim_id,
                "fence": self.fence,
                "work_id": self.work_id,
                "execution_id": self.execution_id,
                "execution_envelope_sha256": (derivation.execution_envelope_sha256),
                "execution_sha256": derivation.execution_sha256,
            },
            inline_evidence={DERIVATION_EVIDENCE_PATH: derivation.to_json_bytes()},
            provenance_builder=provenance_builder,
            idempotency_key=self.execution_id,
            event_context={
                "initiator": {
                    "app": self.producer_app,
                    "claim_id": self.claim_id,
                    "fence": self.fence,
                    "work_id": self.work_id,
                    "execution_id": self.execution_id,
                }
            },
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        return DerivedCollectionReceipt(
            collection_id=receipt.collection_id,
            manifest_sha256=receipt.manifest_sha256,
            content_etag=receipt.content_etag,
            derivation=derivation,
        )


__all__ = ["DerivedCollectionWriter"]
